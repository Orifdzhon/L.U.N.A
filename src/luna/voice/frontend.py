"""Push-to-talk voice front end (ARCHITECTURE sections 4.2, 5).

Toggle model (owner decision D2). One hotkey, the press means depends on the state:

==============  ==========================================================================
IDLE            stop any speech (barge-in), then listen
LISTENING       stop recording now and keep what was said (early stop)
TRANSCRIBING    cancel this turn and listen again
HANDLING        cancel the running request (``cancel`` event) and listen again
==============  ==========================================================================

"Speaking" is owned by the UI (it speaks in ``on_reply``): by then the turn is over, so a
press is an IDLE press and calls ``speaker.stop()`` before the microphone opens.

Threads: ``on_press`` runs on the hotkey listener's worker thread and only flips the state,
signals events and starts a turn thread; it never records, recognises or waits. Each turn
(record -> STT -> orchestrator) runs on its own daemon thread, so a press during processing
can cancel it and start the next turn at once. A superseded turn publishes nothing and never
calls ``on_reply``; it only re-publishes the current status if the orchestrator's late
``IDLE`` would otherwise hide the new ``LISTENING``.

Delivery: a reply is spoken only if its turn is still current; after `on_reply` the turn is
checked again and speech is cut if a press arrived meanwhile. A superseded turn whose
reply is real (an action ran) goes to `on_stale_reply` (print only). At most one STT call is
in flight. Early stop uses a per-turn event passed to `Recorder.record(stop=...)`.

Privacy: the INFO latency line has durations and the provider name only. The transcript is
logged at DEBUG.

Wake mode (slice 4, ``set_live``): with a live source every turn (hotkey, wake, follow-up)
records from a tap of the ``WakeListener`` and holds ``turn:<id>`` on it for the whole turn
(released after ``on_reply``, so it overlaps the ``speaking`` hold). ``on_wake`` is called on the
listener thread, never blocks and accepts only from IDLE. A wake turn is verified locally in
parallel with its recording (ADR 0004 amendment 2): the recording reads through a tee that copies
the first frames after the cut for the verifier, because the Recorder consumes the tap
(``FrameTap.peek`` cannot see frames the Recorder has already read). The status, the cue and
``speaker.stop()`` wait for the verdict; a rejected wake is cancelled without a status, a beep
or any STT call; an error or a timeout fails open. Lock order: front end -> Speaker -> listener.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from luna.audio.capture import SAMPLE_RATE, FrameTap, LiveSource
from luna.audio.recorder import Recorder, Utterance
from luna.config.settings import STTSettings, VoiceSettings, WakeWordSettings
from luna.core.clock import Clock
from luna.core.errors import AudioError, ProviderError
from luna.core.events import EventBus, Status, StatusChanged, TranscriptReady
from luna.core.models import Reply, Request, new_id
from luna.platform.base import HotkeyListener
from luna.stt.base import UNKNOWN_LANG, STTProvider, Transcript
from luna.wakeword.verify import VERIFY_AFTER_MS, VERIFY_TIMEOUT_S, VerifyResult, verify_window

if TYPE_CHECKING:
    from luna.wakeword.listener import WakeTrigger
    from luna.wakeword.verify import WakeVerifier

logger = logging.getLogger(__name__)

DETAIL_NO_SPEECH = "no speech"
DETAIL_NOT_CAUGHT = "didn't catch that"
DETAIL_STT_FAILED = "speech recognition failed"
DETAIL_AUDIO_FAILED = "microphone error"
DEFAULT_CLOSE_TIMEOUT_S = 2.0
FIRST_AUDIO_WAIT_S = 10.0  # how long the turn log waits for the first spoken sound
_FIRST_AUDIO_POLL_S = 0.02
TURN_HOLD_PREFIX = "turn:"
_KEYWORD_TOKEN = re.compile(r"^[\W_]*([^\W\d_]+)")
_KEYWORD_SEPARATORS = " \t\r\n,.:;!?-\u2013\u2014\u2026"


class VoiceState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    HANDLING = "handling"


class _Orchestrator(Protocol):
    def handle(self, request: Request, cancel: threading.Event | None = None) -> Reply: ...


class _Speaker(Protocol):
    def stop(self) -> None: ...

    # Optional (looked up with getattr): ``active`` and ``first_audio_at`` (see Speaker).


class LiveAudio(Protocol):
    """What the front end needs from ``WakeListener`` (ARCHITECTURE section 3)."""

    def open_tap(self, preroll_ms: int, *, skip_ms: int = 0) -> LiveSource: ...

    def set_hold(self, reason: str, active: bool) -> None: ...

    def open_window(self, seconds: float) -> None: ...


@dataclass
class _Verify:
    """Second-stage state of one wake turn (guarded by the front end lock)."""

    pending: bool = True
    outcome: str = "pending"  # accepted | rejected | error | timeout | overridden
    accepted: bool = False
    submitted_at: float | None = None
    ms: float | None = None
    timer: threading.Timer | None = None
    future: Future[VerifyResult] | None = None
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Turn:
    request_id: str
    pressed_at: float
    kind: str = "hotkey"  # hotkey | wake | follow_up
    cancel: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # early stop (keep audio)
    thread: threading.Thread | None = None
    live: LiveAudio | None = None  # the listener this turn records from (None = microphone)
    source: LiveSource | None = None
    open_error: AudioError | None = None
    hold: str | None = None  # turn hold name while held
    verify: _Verify | None = None
    announced: bool = False  # LISTENING was published
    listening_at: float | None = None
    speech_end_at: float | None = None  # recording finished (end of the user's speech)
    reply_at: float | None = None  # reply handed to on_reply (speech starts from here)


class _TeeTap(FrameTap):
    """The turn's tap as the Recorder sees it; copies the first frames it reads for the verifier.

    A ``FrameTap`` subclass that delegates to the listener's tap, so the Recorder's closed /
    error handling is unchanged. ``on_ready`` gets the copied frames once: when ``want`` frames
    were read or the tap is closed, whichever comes first.
    """

    def __init__(self, inner: FrameTap, want: int, on_ready: Callable[[list[bytes]], None]) -> None:
        # No super().__init__(): every FrameTap method is overridden and delegates to ``inner``.
        self._inner = inner
        self._want = max(1, want)
        self._on_ready = on_ready
        self._copied: list[bytes] = []
        self._fired = False
        self._tee_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._inner.closed

    @property
    def dropped(self) -> int:
        return self._inner.dropped

    def push(self, frame: bytes) -> None:
        self._inner.push(frame)

    def read(self, timeout_s: float) -> bytes | None:
        frame = self._inner.read(timeout_s)
        if frame is not None:
            with self._tee_lock:
                if not self._fired:
                    self._copied.append(frame)
                full = len(self._copied) >= self._want
            if full:
                self._fire()
        return frame

    def peek(self, n_frames: int, timeout_s: float) -> list[bytes]:
        return self._inner.peek(n_frames, timeout_s)

    def close(self, error: AudioError | None = None) -> None:
        self._inner.close(error)
        self._fire()

    def _fire(self) -> None:
        with self._tee_lock:
            if self._fired:
                return
            self._fired = True
            frames = list(self._copied)
            self._copied.clear()
        try:
            self._on_ready(frames)
        except Exception:
            logger.warning("starting the wake verification failed", exc_info=True)


class VoiceFrontEnd:
    def __init__(
        self,
        hotkey: HotkeyListener,
        recorder: Recorder,
        stt: STTProvider,
        orchestrator: _Orchestrator,
        speaker: _Speaker | None,
        bus: EventBus,
        on_reply: Callable[[Reply], None],
        *,
        on_stale_reply: Callable[[Reply], None] | None = None,
        voice: VoiceSettings | None = None,
        stt_conf: STTSettings | None = None,
        clock: Clock = time.monotonic,
        beep: Callable[[], None] | None = None,
        verify_timeout_s: float = VERIFY_TIMEOUT_S,
    ) -> None:
        self._hotkey = hotkey
        self._recorder = recorder
        self._stt = stt
        self._orchestrator = orchestrator
        self._speaker = speaker
        self._bus = bus
        self._on_reply = on_reply
        self._on_stale_reply = on_stale_reply  # print only, never speak
        self._stt_lock = threading.Lock()  # at most one recognition in flight
        self._voice = voice or VoiceSettings()
        self._stt_conf = stt_conf or STTSettings()
        self._clock = clock
        self._beep = beep  # hook called right before the microphone opens (start cue)
        self._lock = threading.Lock()
        self._state = VoiceState.IDLE
        self._turn: _Turn | None = None
        self._threads: list[threading.Thread] = []
        self._started = False
        self._hotkey_ok = False
        self._closed = False
        # wake mode (set_live)
        self._live: LiveAudio | None = None
        self._wake = WakeWordSettings()
        self._verifier: WakeVerifier | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._verify_timeout_s = verify_timeout_s
        self._rec_lock = threading.Lock()  # live mode: one recording at a time
        self._warned: set[str] = set()
        self._wake_stats = {"detections": 0, "accepted": 0, "rejected": 0}  # this session

    @property
    def state(self) -> VoiceState:
        return self._state

    # ---- lifecycle -----------------------------------------------------------------

    def start(self, *, hotkey_required: bool = True) -> bool:
        """Register the hotkey. Raises ``ToolError`` if it is invalid or already taken.

        With ``hotkey_required=False`` (wake mode) a registration failure is logged once and the
        front end still starts, so wake turns work without the hotkey. Returns True when the
        hotkey is registered.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("VoiceFrontEnd is closed")
            if self._started:
                return self._hotkey_ok
        registered = True
        try:
            self._hotkey.start(self._voice.hotkey, self._on_press)
        except Exception as exc:
            if hotkey_required:
                raise
            registered = False
            logger.warning(
                "hotkey %s unavailable (%s); wake mode works without it",
                self._voice.hotkey,
                type(exc).__name__,
            )
        with self._lock:
            self._started = True
            self._hotkey_ok = registered
        return registered

    def stop(self) -> None:
        """Unregister the hotkey and cancel the active turn (if any). Idempotent."""
        with self._lock:
            self._started = False
            turn = self._turn
            if turn is not None:
                turn.cancel.set()
                self._drop_verify(turn)
                self._turn = None
                self._set_state(VoiceState.IDLE, turn.request_id, publish=turn.announced)
        self._hotkey.stop()

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_S) -> None:
        """Stop everything and join the turn threads (an STT call in flight is abandoned)."""
        with self._lock:
            self._closed = True
            executor, self._executor = self._executor, None
            self._verifier = None
            self._live = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        self.stop()
        deadline = time.monotonic() + timeout
        me = threading.current_thread()
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if thread is not me:
                # Real time on purpose: the injected clock may be a manual one.
                thread.join(max(0.0, deadline - time.monotonic()))

    # ---- wake mode ------------------------------------------------------------------

    def set_live(
        self,
        live: LiveAudio | None,
        wake: WakeWordSettings | None = None,
        verifier: WakeVerifier | None = None,
    ) -> None:
        """Record every turn from ``live`` (wake mode) or from the microphone (``None``).

        ``verifier`` None or a pass-through ("off") means no second stage. ``set_live(None)``
        cancels a turn that is still listening on a tap; later stages finish normally.
        """
        with self._lock:
            old, self._executor = self._executor, None
            self._verifier = None
            self._live = live
            self._wake = wake or WakeWordSettings()
            if live is not None and verifier is not None and verifier.name != "off":
                self._verifier = verifier
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="luna-verify")
            turn = self._turn
            if (
                turn is not None
                and turn.live is not None
                and self._state is VoiceState.LISTENING
                and (live is None or (turn.verify is not None and turn.verify.pending))
            ):
                turn.cancel.set()
                self._drop_verify(turn)
                self._turn = None
                self._set_state(VoiceState.IDLE, turn.request_id, publish=turn.announced)
        if old is not None:
            old.shutdown(wait=False, cancel_futures=True)

    def on_wake(self, trigger: WakeTrigger) -> bool:
        """Start a wake (or follow-up) turn. Listener thread: never blocks, IDLE only."""
        with self._lock:
            if self._closed or not self._started or self._live is None:
                return False
            if self._state is not VoiceState.IDLE or self._turn is not None:
                return False
            kind = "wake" if trigger.event is not None else "follow_up"
            if kind == "wake":
                self._wake_stats["detections"] += 1
            self._begin_turn(kind, trigger)
            return True

    @property
    def wake_stats(self) -> dict[str, int]:
        """Session counters: first-stage detections and the verifier's verdicts."""
        return dict(self._wake_stats)

    # ---- hotkey (background thread of the listener) ---------------------------------

    def _on_press(self) -> None:
        with self._lock:
            if self._closed or not self._started:
                return
            turn = self._turn
            if self._state is VoiceState.LISTENING and turn is not None:
                if turn.verify is not None and turn.verify.pending:
                    # Unverified wake: the press overrides it and starts a normal hotkey turn.
                    turn.cancel.set()
                    self._drop_verify(turn)
                    self._begin_turn()
                    return
                turn.stop.set()  # per turn: also applies if record() has not started yet
                return
            if turn is not None:  # TRANSCRIBING / HANDLING: cancel it, then listen again
                turn.cancel.set()
            self._begin_turn()

    def _begin_turn(self, kind: str = "hotkey", trigger: WakeTrigger | None = None) -> None:
        """Start a turn (call with the lock held). Speech is stopped before the mic opens."""
        live = self._live
        source = trigger.source if trigger is not None else None
        verifying = (
            kind == "wake"
            and self._verifier is not None
            and source is not None
            and isinstance(source.frames, FrameTap)
        )
        barge = False
        if kind == "hotkey" or (kind == "wake" and not verifying):
            barge = bool(getattr(self._speaker, "active", False))
            self._stop_speech()
        now = self._clock()
        turn = _Turn(new_id(), trigger.detected_at if trigger is not None else now, kind=kind)
        turn.live = live
        if live is not None:
            turn.hold = TURN_HOLD_PREFIX + turn.request_id
            live.set_hold(turn.hold, True)
        try:
            if live is not None and kind == "hotkey":
                try:
                    turn.source = (
                        live.open_tap(0, skip_ms=self._wake.speaking_tail_ms)
                        if barge
                        else live.open_tap(self._voice.preroll_ms)
                    )
                except AudioError as exc:
                    turn.open_error = exc
            elif source is not None:
                turn.source = source
            self._turn = turn
            if verifying:
                assert source is not None and isinstance(source.frames, FrameTap)
                turn.verify = _Verify()
                want = -(-VERIFY_AFTER_MS // self._voice.frame_ms)
                tee = _TeeTap(source.frames, want, lambda frames: self._submit_verify(turn, frames))
                turn.source = dataclasses.replace(source, frames=tee)
                self._state = VoiceState.LISTENING  # announced after the verdict
            else:
                turn.announced = True
                turn.listening_at = now
                self._set_state(VoiceState.LISTENING, turn.request_id, publish=True)
            thread = threading.Thread(
                target=self._run_turn, args=(turn,), name="luna-voice", daemon=True
            )
            turn.thread = thread
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
            thread.start()
        except BaseException:
            self._drop_verify(turn)
            if self._turn is turn:
                self._turn = None
                self._state = VoiceState.IDLE
            self._release_hold(turn)
            raise

    def _stop_speech(self) -> None:
        if self._speaker is None:
            return
        try:
            self._speaker.stop()
        except Exception:
            logger.warning("stopping speech failed", exc_info=True)

    def _release_hold(self, turn: _Turn) -> None:
        """Idempotent; safe on every exit path (takes the listener lock, never the front end's)."""
        name, turn.hold = turn.hold, None
        if name is not None and turn.live is not None:
            try:
                turn.live.set_hold(name, False)
            except Exception:
                logger.warning("releasing the turn hold failed", exc_info=True)

    # ---- wake verification (ADR 0004 amendment 2) ----------------------------------

    def _drop_verify(self, turn: _Turn) -> None:
        """Lock held. The turn no longer waits for a verdict; a late result is ignored."""
        v = turn.verify
        if v is None or not v.pending:
            return
        v.pending = False
        v.outcome = "overridden"
        if v.timer is not None:
            v.timer.cancel()
        if v.future is not None:
            v.future.cancel()  # drops work still queued behind a running decode
        v.done.set()

    def _submit_verify(self, turn: _Turn, frames: list[bytes]) -> None:
        """Recorder thread (tee) or close path: hand the window to the ``luna-verify`` worker."""
        with self._lock:
            v = turn.verify
            executor = self._executor
            verifier = self._verifier
            source = turn.source
            if (
                v is None
                or not v.pending
                or v.submitted_at is not None
                or turn.cancel.is_set()
                or executor is None
                or verifier is None
                or source is None
            ):
                return
            v.submitted_at = self._clock()
            timer = threading.Timer(self._verify_timeout_s, self._on_verify_timeout, (turn,))
            timer.daemon = True
            v.timer = timer
        window = verify_window(source.lead_in, frames)
        timer.start()
        try:
            future = executor.submit(verifier.verify, window)
        except RuntimeError:  # executor shut down meanwhile (set_live / close)
            self._settle(turn, True, "error")
            return
        with self._lock:
            v.future = future
            stale = not v.pending
        if stale:
            future.cancel()
        future.add_done_callback(lambda f: self._on_verdict(turn, f))

    def _on_verdict(self, turn: _Turn, future: Future[VerifyResult]) -> None:
        if future.cancelled():
            return
        try:
            result = future.result()
        except Exception as exc:
            if self._settle(turn, True, "error"):
                self._warn_once("error", f"wake verification failed ({type(exc).__name__})")
            return
        if result.text and result.accepted:  # a rejected wake leaves no trace of its audio
            logger.debug("Verifier heard: %r", result.text)
        log_rejected = bool(result.text) and not result.accepted and self._wake.log_rejects
        if result.reason == "error":
            outcome = "error"
        else:
            outcome = "accepted" if result.accepted else "rejected"
        settled = self._settle(turn, result.accepted, outcome)
        if settled and log_rejected:  # opt-in ([wakeword] log_rejects): local log only
            logger.info("wake rejected, verifier heard: %r", result.text)
        if settled and outcome == "error":
            self._warn_once("error", "wake verification error; first stage only")

    def _on_verify_timeout(self, turn: _Turn) -> None:
        if self._settle(turn, True, "timeout"):
            self._warn_once("timeout", "wake verification timed out; accepting")

    def _warn_once(self, reason: str, message: str) -> None:
        with self._lock:
            first = reason not in self._warned
            self._warned.add(reason)
        if first:
            logger.warning(message)

    def _settle(self, turn: _Turn, accepted: bool, outcome: str) -> bool:
        """Apply the verdict once; True only if it was applied (False: late or overridden)."""
        with self._lock:
            v = turn.verify
            if v is None or not v.pending:
                return False
            v.pending = False
            self._wake_stats["accepted" if accepted else "rejected"] += 1
            if v.timer is not None:
                v.timer.cancel()
            if v.submitted_at is not None:
                v.ms = (self._clock() - v.submitted_at) * 1000
            if self._turn is not turn or turn.cancel.is_set():
                v.outcome = "overridden"
                v.done.set()
                return False
            v.outcome = outcome
            v.accepted = accepted
            if accepted:
                turn.announced = True
                turn.listening_at = self._clock()
                self._bus.publish(StatusChanged(Status.LISTENING, turn.request_id))
            else:  # silent cancel: no status, no beep, nothing recognised or sent
                turn.cancel.set()
                self._turn = None
                self._state = VoiceState.IDLE
            v.done.set()
        if accepted:
            self._stop_speech()
            self._cue()
        else:
            logger.info("wake rejected verify_ms=%.0f", v.ms or 0.0)
        return True

    def _cue(self) -> None:
        if self._beep is None or self._wake.cue != "beep":
            return
        try:
            self._beep()
        except Exception:
            logger.warning("start cue failed", exc_info=True)

    def _await_verdict(self, turn: _Turn) -> bool:
        """After the recording: wait for the verdict. False = the turn must end silently."""
        v = turn.verify
        if v is None:
            return True
        deadline = time.monotonic() + self._verify_timeout_s + 1.0
        while not v.done.wait(0.02):
            if turn.cancel.is_set():
                return False
            if time.monotonic() > deadline:
                self._settle(turn, True, "timeout")
                break
        return v.accepted and not turn.cancel.is_set()

    # ---- state helpers (lock held unless noted) ------------------------------------

    def _set_state(
        self, state: VoiceState, rid: str, *, publish: bool, detail: str | None = None
    ) -> None:
        self._state = state
        if not publish:
            return
        status = {
            VoiceState.IDLE: Status.IDLE,
            VoiceState.LISTENING: Status.LISTENING,
            VoiceState.TRANSCRIBING: Status.THINKING,
            VoiceState.HANDLING: Status.THINKING,
        }[state]
        self._bus.publish(StatusChanged(status, rid, detail))

    def _advance(self, turn: _Turn, state: VoiceState, *, publish: bool = True) -> bool:
        """Move the current turn to ``state``; False if the turn was superseded or cancelled."""
        with self._lock:
            if self._turn is not turn or turn.cancel.is_set():
                return False
            self._set_state(state, turn.request_id, publish=publish)
            return True

    def _end(self, turn: _Turn, status: Status, detail: str | None = None) -> None:
        """Finish the turn with a final status (IDLE or ERROR); no-op if superseded."""
        with self._lock:
            if self._turn is not turn:
                return
            self._turn = None
            self._state = VoiceState.IDLE
            self._bus.publish(StatusChanged(status, turn.request_id, detail))

    def _republish_current(self) -> None:
        """A superseded turn ended: restore the status that its late events may have hidden."""
        with self._lock:
            turn = self._turn
            if turn is None or self._closed or not turn.announced:
                return
            if self._state is VoiceState.LISTENING:
                self._bus.publish(StatusChanged(Status.LISTENING, turn.request_id))
            else:
                self._bus.publish(StatusChanged(Status.THINKING, turn.request_id))

    # ---- one turn (own thread) ------------------------------------------------------

    def _run_turn(self, turn: _Turn) -> None:
        spans: dict[str, float] = {}
        provider = ""
        reason = "-"
        try:
            utt = self._record(turn, spans)
            if utt is None:
                return
            reason = utt.reason
            if utt.reason == "cancelled":  # also: the listener closed the tap; no verdict wait
                return
            if not self._await_verdict(turn):
                return
            if turn.cancel.is_set() or utt.reason == "cancelled":
                return
            quiet = turn.kind != "hotkey"  # wake / follow-up: end without a message
            if utt.reason == "no_speech" or not utt.pcm16:
                self._end(turn, Status.IDLE, None if quiet else DETAIL_NO_SPEECH)
                return
            if not self._advance(turn, VoiceState.TRANSCRIBING):
                return
            transcript = self._transcribe(turn, utt, spans)
            del utt  # drop the audio as soon as it was recognised
            if transcript is None:
                return
            provider = transcript.provider
            if turn.cancel.is_set():
                return
            if turn.kind == "wake":
                transcript = self._strip_keyword(transcript)
            if transcript.is_empty:
                self._end(turn, Status.IDLE, None if quiet else DETAIL_NOT_CAUGHT)
                return
            logger.debug("Transcript: %r", transcript.text)
            self._handle(turn, transcript, spans)
        except Exception:
            logger.exception("voice turn failed")
            self._end(turn, Status.ERROR, "voice error")
        finally:
            try:
                with self._lock:
                    self._drop_verify(turn)
                handled = "handle_ms" in spans
                self._end_if_current(turn, handled=handled)
                if turn.cancel.is_set() and handled:
                    self._republish_current()
            finally:
                self._release_hold(turn)  # after on_reply: holds overlap with "speaking"
                # Last: it may wait (daemon turn thread) for the first spoken sound.
                self._log_spans(turn, spans, provider, reason)

    def _strip_keyword(self, transcript: Transcript) -> Transcript:
        """Drop a leading address word ("Луна, ..."); the rest is the command."""
        match = _KEYWORD_TOKEN.match(transcript.text)
        if match is None:
            return transcript
        words = {_fold(w) for w in (*self._wake.keywords, *self._wake.verify_words)}
        if _fold(match.group(1)) not in words:
            return transcript
        rest = transcript.text[match.end() :].lstrip(_KEYWORD_SEPARATORS)
        return dataclasses.replace(transcript, text=rest)

    def _end_if_current(self, turn: _Turn, *, handled: bool) -> None:
        """Safety net: a turn that returned without a final status must not block the next.

        After ``handle`` the orchestrator already published the final IDLE; a second one
        could hide a SPEAKING status the UI set meanwhile, so the state is reset silently.
        """
        with self._lock:
            if self._turn is turn:
                self._turn = None
                self._state = VoiceState.IDLE
                if not handled and turn.announced:  # an unannounced wake turn ends silently
                    self._bus.publish(StatusChanged(Status.IDLE, turn.request_id))

    def _record(self, turn: _Turn, spans: dict[str, float]) -> Utterance | None:
        if turn.open_error is not None:
            logger.warning("Recording failed: %s", turn.open_error)
            self._end(turn, Status.ERROR, DETAIL_AUDIO_FAILED)
            return None
        beep: Callable[[], None] | None = None
        if turn.kind == "hotkey":
            spans["hotkey_to_listening_ms"] = (self._clock() - turn.pressed_at) * 1000
            beep = self._beep
        elif turn.kind == "wake" and turn.verify is None and self._wake.cue == "beep":
            beep = self._beep  # with verification the cue waits for the verdict (_settle)
        if beep is not None:
            try:
                beep()
            except Exception:
                logger.warning("start cue failed", exc_info=True)
        started = self._clock()
        try:
            if turn.source is None:
                utt = self._recorder.record(turn.cancel, stop=turn.stop)
            else:
                utt = self._record_live(turn)
        except AudioError as exc:
            logger.warning("Recording failed: %s", exc)
            self._end(turn, Status.ERROR, DETAIL_AUDIO_FAILED)
            return None
        turn.speech_end_at = self._clock()
        spans["capture_ms"] = (turn.speech_end_at - started) * 1000
        return utt

    def _record_live(self, turn: _Turn) -> Utterance:
        """Record from the turn's tap; the shared Recorder and its VAD serve one turn at a time."""
        source = turn.source
        assert source is not None
        while not self._rec_lock.acquire(timeout=0.05):
            if turn.cancel.is_set():
                break
        else:
            try:
                if not turn.cancel.is_set():
                    if turn.kind == "hotkey":
                        return self._recorder.record(turn.cancel, stop=turn.stop, live=source)
                    return self._recorder.record(
                        turn.cancel,
                        stop=turn.stop,
                        live=source,
                        no_speech_s=self._wake.command_timeout_s,
                    )
            finally:
                self._rec_lock.release()
        close = getattr(source.frames, "close", None)  # never recorded: free the tap
        if close is not None:
            close()
        return Utterance(b"", SAMPLE_RATE, 0.0, "cancelled")

    def _transcribe(
        self, turn: _Turn, utt: Utterance, spans: dict[str, float]
    ) -> Transcript | None:
        language = self._stt_conf.language
        while not self._stt_lock.acquire(timeout=0.05):  # a superseded turn may still be busy
            if turn.cancel.is_set():
                return None
        started = self._clock()
        try:
            transcript = self._stt.transcribe(
                utt.pcm16,
                utt.sample_rate,
                lang_hint=None if language == "auto" else language,
                prompt=self._stt_conf.prompt or None,
                timeout_s=self._stt_conf.timeout_s,
            )
        except ProviderError as exc:
            logger.warning("Speech recognition failed: %s", type(exc).__name__)
            self._end(turn, Status.ERROR, DETAIL_STT_FAILED)
            return None
        finally:
            spans["stt_ms"] = (self._clock() - started) * 1000
            self._stt_lock.release()
        return transcript

    def _handle(self, turn: _Turn, transcript: Transcript, spans: dict[str, float]) -> None:
        lang = None if transcript.lang == UNKNOWN_LANG else transcript.lang
        request = Request(
            text=transcript.text,
            source="voice",
            lang=lang,
            request_id=turn.request_id,
        )
        if not self._advance(turn, VoiceState.HANDLING, publish=False):
            return
        self._bus.publish(TranscriptReady(turn.request_id, transcript.text, lang))
        started = self._clock()
        try:
            reply = self._orchestrator.handle(request, turn.cancel)
        except Exception:
            logger.exception("Handling the voice request failed")
            self._end(turn, Status.ERROR, "request failed")
            return
        finally:
            spans["handle_ms"] = (self._clock() - started) * 1000
        if self._superseded(turn):
            self._deliver_stale(reply)  # the user moved on: never speak it
            return
        turn.reply_at = self._clock()
        try:
            self._on_reply(reply)
        except Exception:
            logger.warning("on_reply failed", exc_info=True)
        # A press may have landed while on_reply was starting the speech: cut it again.
        if self._superseded(turn) and self._speaker is not None:
            try:
                self._speaker.stop()
            except Exception:
                logger.warning("stopping speech failed", exc_info=True)

    def _first_audio_spans(self, turn: _Turn) -> dict[str, float]:
        """``tts_first_audio_ms`` (reply ready -> first sound) and ``total_ms`` (end of speech
        -> first sound), once the speaker started playing this turn's reply.

        Waits (bounded) while the speaker is still preparing the reply; gives up when the turn
        is cancelled, the front end closes or the speaker went idle without a sound.
        """
        speaker = self._speaker
        reply_at = turn.reply_at
        if speaker is None or reply_at is None:
            return {}
        deadline = time.monotonic() + FIRST_AUDIO_WAIT_S
        while True:
            at = getattr(speaker, "first_audio_at", None)
            if isinstance(at, float) and at >= reply_at:
                out = {"tts_first_audio_ms": (at - reply_at) * 1000}
                if turn.speech_end_at is not None:
                    out["total_ms"] = (at - turn.speech_end_at) * 1000
                return out
            if (
                self._closed
                or turn.cancel.is_set()
                or getattr(speaker, "active", False) is not True
                or time.monotonic() >= deadline
            ):
                return {}
            time.sleep(_FIRST_AUDIO_POLL_S)

    def _superseded(self, turn: _Turn) -> bool:
        with self._lock:
            return turn.cancel.is_set() or self._turn is not turn

    def _deliver_stale(self, reply: Reply) -> None:
        """A cancel arrived but the work finished (an action really ran): show, don't speak.

        The orchestrator's own apology for a cancelled turn has no provider and no actions
        and is dropped.
        """
        if self._on_stale_reply is None or (reply.provider is None and not reply.actions):
            return
        try:
            self._on_stale_reply(reply)
        except Exception:
            logger.warning("on_stale_reply failed", exc_info=True)

    def _log_spans(self, turn: _Turn, spans: dict[str, float], provider: str, reason: str) -> None:
        if "stt_ms" not in spans:
            return  # nothing was recognised: not a completed turn
        spans = dict(spans)
        v = turn.verify
        if v is not None and v.ms is not None:
            spans["verify_ms"] = v.ms
        spans.update(self._first_audio_spans(turn))
        if turn.kind != "hotkey" and turn.listening_at is not None:
            spans["wake_to_listening_ms"] = (turn.listening_at - turn.pressed_at) * 1000
        parts = " ".join(f"{k}={val:.0f}" for k, val in spans.items())
        verify = v.outcome if v is not None else ("off" if turn.kind == "wake" else "-")
        logger.info(
            "voice turn %s: trigger=%s verify=%s %s provider=%s end=%s",
            turn.request_id[:8],
            turn.kind,
            verify,
            parts,
            provider or "-",
            reason,
        )


def _fold(word: str) -> str:
    return word.strip().casefold().replace("\u0451", "\u0435")
