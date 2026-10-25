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
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from luna.audio.recorder import Recorder, Utterance
from luna.config.settings import STTSettings, VoiceSettings
from luna.core.clock import Clock
from luna.core.errors import AudioError, ProviderError
from luna.core.events import EventBus, Status, StatusChanged, TranscriptReady
from luna.core.models import Reply, Request, new_id
from luna.platform.base import HotkeyListener
from luna.stt.base import UNKNOWN_LANG, STTProvider, Transcript

logger = logging.getLogger(__name__)

DETAIL_NO_SPEECH = "no speech"
DETAIL_NOT_CAUGHT = "didn't catch that"
DETAIL_STT_FAILED = "speech recognition failed"
DETAIL_AUDIO_FAILED = "microphone error"
DEFAULT_CLOSE_TIMEOUT_S = 2.0


class VoiceState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    HANDLING = "handling"


class _Orchestrator(Protocol):
    def handle(self, request: Request, cancel: threading.Event | None = None) -> Reply: ...


class _Speaker(Protocol):
    def stop(self) -> None: ...


@dataclass
class _Turn:
    request_id: str
    pressed_at: float
    cancel: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # early stop (keep audio)
    thread: threading.Thread | None = None


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
        self._closed = False

    @property
    def state(self) -> VoiceState:
        return self._state

    # ---- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Register the hotkey. Raises ``ToolError`` if it is invalid or already taken."""
        with self._lock:
            if self._closed:
                raise RuntimeError("VoiceFrontEnd is closed")
            if self._started:
                return
        self._hotkey.start(self._voice.hotkey, self._on_press)
        with self._lock:
            self._started = True

    def stop(self) -> None:
        """Unregister the hotkey and cancel the active turn (if any). Idempotent."""
        with self._lock:
            self._started = False
            turn = self._turn
            if turn is not None:
                turn.cancel.set()
                self._turn = None
                self._set_state(VoiceState.IDLE, turn.request_id, publish=True)
        self._hotkey.stop()

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_S) -> None:
        """Stop everything and join the turn threads (an STT call in flight is abandoned)."""
        with self._lock:
            self._closed = True
        self.stop()
        deadline = time.monotonic() + timeout
        me = threading.current_thread()
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if thread is not me:
                # Real time on purpose: the injected clock may be a manual one.
                thread.join(max(0.0, deadline - time.monotonic()))

    # ---- hotkey (background thread of the listener) ---------------------------------

    def _on_press(self) -> None:
        with self._lock:
            if self._closed or not self._started:
                return
            turn = self._turn
            if self._state is VoiceState.LISTENING and turn is not None:
                turn.stop.set()  # per turn: also applies if record() has not started yet
                return
            if turn is not None:  # TRANSCRIBING / HANDLING: cancel it, then listen again
                turn.cancel.set()
            self._begin_turn()

    def _begin_turn(self) -> None:
        """Start listening (call with the lock held). Speech is stopped before the mic opens."""
        if self._speaker is not None:
            try:
                self._speaker.stop()
            except Exception:
                logger.warning("stopping speech failed", exc_info=True)
        turn = _Turn(new_id(), self._clock())
        self._turn = turn
        self._set_state(VoiceState.LISTENING, turn.request_id, publish=True)
        thread = threading.Thread(
            target=self._run_turn, args=(turn,), name="luna-voice", daemon=True
        )
        turn.thread = thread
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()

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
            if turn is None or self._closed:
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
            if turn.cancel.is_set() or utt.reason == "cancelled":
                return
            if utt.reason == "no_speech" or not utt.pcm16:
                self._end(turn, Status.IDLE, DETAIL_NO_SPEECH)
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
            if transcript.is_empty:
                self._end(turn, Status.IDLE, DETAIL_NOT_CAUGHT)
                return
            logger.debug("Transcript: %r", transcript.text)
            self._handle(turn, transcript, spans)
        except Exception:
            logger.exception("voice turn failed")
            self._end(turn, Status.ERROR, "voice error")
        finally:
            self._log_spans(turn, spans, provider, reason)
            handled = "handle_ms" in spans
            self._end_if_current(turn, handled=handled)
            if turn.cancel.is_set() and handled:
                self._republish_current()

    def _end_if_current(self, turn: _Turn, *, handled: bool) -> None:
        """Safety net: a turn that returned without a final status must not block the next.

        After ``handle`` the orchestrator already published the final IDLE; a second one
        could hide a SPEAKING status the UI set meanwhile, so the state is reset silently.
        """
        with self._lock:
            if self._turn is turn:
                self._turn = None
                self._state = VoiceState.IDLE
                if not handled:
                    self._bus.publish(StatusChanged(Status.IDLE, turn.request_id))

    def _record(self, turn: _Turn, spans: dict[str, float]) -> Utterance | None:
        spans["hotkey_to_listening_ms"] = (self._clock() - turn.pressed_at) * 1000
        if self._beep is not None:
            try:
                self._beep()
            except Exception:
                logger.warning("start cue failed", exc_info=True)
        started = self._clock()
        try:
            utt = self._recorder.record(turn.cancel, stop=turn.stop)
        except AudioError as exc:
            logger.warning("Recording failed: %s", exc)
            self._end(turn, Status.ERROR, DETAIL_AUDIO_FAILED)
            return None
        spans["capture_ms"] = (self._clock() - started) * 1000
        return utt

    def _transcribe(
        self, turn: _Turn, utt: Utterance, spans: dict[str, float]
    ) -> Transcript | None:
        hints = self._stt_conf.language_hints
        while not self._stt_lock.acquire(timeout=0.05):  # a superseded turn may still be busy
            if turn.cancel.is_set():
                return None
        started = self._clock()
        try:
            transcript = self._stt.transcribe(
                utt.pcm16,
                utt.sample_rate,
                lang_hint=hints[0] if len(hints) == 1 else None,
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
        parts = " ".join(f"{k}={v:.0f}" for k, v in spans.items())
        logger.info(
            "voice turn %s: %s provider=%s end=%s",
            turn.request_id[:8],
            parts,
            provider or "-",
            reason,
        )
