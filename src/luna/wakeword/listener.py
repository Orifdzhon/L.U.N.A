"""Always-on wake listener (ARCHITECTURE sections 3, 4.2 and 5; ADR 0004).

``WakeListener`` is the only owner and reader of the microphone in wake mode. One thread
(``luna-wake``) reads 30 ms frames and, for each of them:

1. counts samples (``position``) and appends the frame to a 2 s ring buffer;
2. forwards it to the open ``FrameTap`` (the ``Recorder`` of a running turn), if any;
3. runs the energy VAD; the keyword detector only sees audio inside a *speech segment*
   (300 ms of pre-roll, then every frame until 600 ms of silence or 10 s, then ``reset()``);
   so a silent microphone costs almost nothing;
4. on a ``WakeEvent`` cuts the audio at the end of the keyword: the second before the cut is
   the ``lead_in``, everything after it is the tap backlog, and ``on_wake`` is called.

Detection is muted (state ``HELD``) while a *hold* is active (the assistant speaks, a turn
runs), for a tail after speech, and for ``cooldown_s`` after a detection. Returning to
``ARMED`` resets the detector and clears the ring, so audio of the assistant's own voice can
never complete a keyword later.

Locking: one lock guards the state. The detector (``process`` and ``reset``), ``on_wake`` and
bus publishing are never called with it held, so callbacks may call back into the listener and
``set_hold`` never waits for the engine. ``stop()`` joins the thread and must not be called
while holding a lock that ``on_wake`` needs.

``on_wake`` runs on the mic thread: it must return quickly (submit the turn and return), because
the microphone is not read meanwhile. The VAD is owned by the listener (see ``vad_factory``).
A ``WakeStateChanged`` is published on the bus although it is not part of the ``Event`` union
(the bus is untyped at runtime; subscribers should ignore unknown events).

Privacy: audio exists only in the in-memory ring and the tap queue; nothing is stored, sent
or logged (counters only).
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from luna.audio.base import SAMPLE_WIDTH_BYTES
from luna.audio.capture import (
    DEFAULT_FRAME_MS,
    SAMPLE_RATE,
    FrameTap,
    LiveSource,
    MicSource,
    RingBuffer,
    frame_bytes,
)
from luna.audio.vad import VoiceActivityDetector
from luna.config.settings import VoiceSettings, WakeWordSettings
from luna.core.clock import Clock
from luna.core.errors import AudioError
from luna.core.events import EventBus
from luna.wakeword.base import WakeEvent, WakeWordDetector

log = logging.getLogger(__name__)

RING_MS = 2000  # audio kept in memory before a keyword
GATE_PREROLL_MS = 300  # audio fed to the detector before the VAD opened a segment
SEGMENT_SILENCE_MS = 600  # a segment closes after this much non-speech
SEGMENT_MAX_MS = 10_000  # ... or when it gets this long (the detector then restarts)
LEAD_IN_MS = 1000  # context kept before the cut
FALLBACK_LAG_MS = 250  # cut when the engine reports no end_sample: fed audio minus this
STALL_S = 2.0  # no frame from the microphone for this long -> FAILED
READ_TIMEOUT_S = 0.1
JOIN_TIMEOUT_S = 5.0
SPEAKING_HOLD = "speaking"
TURN_HOLD_PREFIX = "turn:"
FRAME_BYTES = frame_bytes(DEFAULT_FRAME_MS)


class WakeState(enum.Enum):
    OFF = "off"  # mic closed
    ARMED = "armed"  # detector fed (inside VAD speech segments)
    HELD = "held"  # stream open, detector muted (speaking / turn / tail / cooldown)
    FAILED = "failed"  # mic lost or detector error; mic closed; start() retries


@dataclass(frozen=True)
class WakeTrigger:
    source: LiveSource  # tap with the backlog after the cut + lead_in before it
    event: WakeEvent | None  # None = speech onset in a follow-up window
    detected_at: float  # clock time, for the wake_to_listening_ms span


@dataclass(frozen=True)
class WakeStateChanged:
    """Published on the ``EventBus`` for the indicator."""

    state: WakeState
    detail: str | None = None  # "speaking", "turn", "tail", "cooldown", "follow-up", a reason


class WakeGate:
    """Pure, clock-driven hold / tail / cooldown / follow-up window logic (no threads, no I/O)."""

    def __init__(self) -> None:
        self._holds: set[str] = set()
        self._tail_until = 0.0
        self._cooldown_until = 0.0
        self._window_pending_s: float | None = None
        self._window_until: float | None = None

    @property
    def holds(self) -> frozenset[str]:
        return frozenset(self._holds)

    def reset(self) -> None:
        self.__init__()

    def set_hold(self, name: str, active: bool, now: float, tail_s: float = 0.0) -> None:
        """Idempotent. Releasing a hold keeps the detector muted for ``tail_s`` more."""
        if active:
            self._holds.add(name)
        elif name in self._holds:
            self._holds.discard(name)
            self._tail_until = max(self._tail_until, now + tail_s)

    def start_cooldown(self, now: float, cooldown_s: float) -> None:
        self._cooldown_until = max(self._cooldown_until, now + cooldown_s)

    def hold_reason(self, now: float) -> str | None:
        """Why the detector is muted right now, or None when it may listen."""
        if self._holds:
            name = min(self._holds)
            return "turn" if name.startswith(TURN_HOLD_PREFIX) else name
        if now < self._tail_until:
            return "tail"
        if now < self._cooldown_until:
            return "cooldown"
        return None

    # --- follow-up window (#113): starts when the listener is armed, expires on its own -----

    def open_window(self, seconds: float, now: float) -> None:
        """``seconds <= 0`` closes it. The clock starts once nothing holds the detector."""
        if seconds <= 0:
            self._window_pending_s = None
            self._window_until = None
            return
        self._window_pending_s = seconds
        self._window_until = None
        self.update_window(now)

    def update_window(self, now: float) -> None:
        if self._window_pending_s is not None and self.hold_reason(now) is None:
            self._window_until = now + self._window_pending_s
            self._window_pending_s = None

    def window_open(self, now: float) -> bool:
        """True while speech onset may fire a follow-up trigger."""
        return self._window_until is not None and now < self._window_until

    @property
    def window_deadline(self) -> float | None:
        return self._window_until

    def restore_window(self, deadline: float | None) -> None:
        """Give back a window consumed by a refused trigger (it keeps its original deadline)."""
        if deadline is not None and self._window_until is None:
            self._window_until = deadline

    def window_pending(self) -> bool:
        return self._window_pending_s is not None

    def poll_window_expired(self, now: float) -> bool:
        """True exactly once when a started window ran out."""
        if self._window_until is not None and now >= self._window_until:
            self._window_until = None
            return True
        return False


class WakeListener:
    """Implements ``LiveAudio``: owns the always-on stream, detects the keyword, serves taps."""

    def __init__(
        self,
        mic: MicSource,
        detector: WakeWordDetector,
        vad_factory: Callable[[], VoiceActivityDetector],
        conf: WakeWordSettings,
        voice: VoiceSettings,
        bus: EventBus,
        *,
        on_wake: Callable[[WakeTrigger], bool],
        clock: Clock = time.monotonic,
        stall_s: float = STALL_S,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        self._mic = mic
        self._detector = detector
        # The VAD keeps per-stream state (noise floor, hangover) and is called on every frame by
        # luna-wake only; the Recorder needs its own instance, hence a factory, not an instance.
        self._vad = vad_factory()
        self._conf = conf
        self._voice = voice
        self._bus = bus
        self._on_wake = on_wake
        self._clock = clock
        self._stall_s = stall_s
        self._read_timeout_s = read_timeout_s
        self._det_bytes = detector.frame_samples * SAMPLE_WIDTH_BYTES

        self._lock = threading.Lock()
        self._start_lock = threading.Lock()  # serialises start(); stop() never takes it
        self._need_reset = False  # detector.reset() requested under the lock, done outside it
        self._ring = RingBuffer(RING_MS)
        self._gate = WakeGate()
        self._state = WakeState.OFF
        self._position = 0
        self._tap: FrameTap | None = None
        self._skip_samples = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._closed = False
        self._speech_run = 0  # consecutive speech samples (follow-up window onset)
        # Segment state: touched only by the luna-wake thread.
        self._seg_open = False
        self._seg_base = 0  # absolute sample index of the detector's sample 0
        self._seg_silence = 0
        self._seg_samples = 0
        self._fed = 0  # samples handed to the detector since its last reset
        self._pending = bytearray()  # tail of the stream not yet a full detector frame

    # ------------------------------------------------------------------ introspection

    @property
    def state(self) -> WakeState:
        return self._state

    @property
    def position(self) -> int:
        """Samples read since ``start()`` (absolute stream position)."""
        return self._position

    @property
    def noise_floor(self) -> float | None:
        value = getattr(self._vad, "noise_floor", None)
        return float(value) if value is not None else None

    @property
    def buffered_bytes(self) -> int:
        """Size of the in-memory ring (capped at about ``RING_MS`` of audio)."""
        return len(self._ring)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Open the microphone and start ``luna-wake``. Idempotent. Raises ``AudioError``."""
        with self._start_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        with self._lock:
            if self._closed:
                raise AudioError("wake listener is closed")
            if self._state in (WakeState.ARMED, WakeState.HELD):
                return
            old = self._thread
        if old is not None and old is not threading.current_thread():
            old.join(JOIN_TIMEOUT_S)
            if old.is_alive():  # stuck in the detector: two threads must never share it
                raise AudioError("the previous wake listener thread is still running")
        self._mic.start()  # AudioError propagates; nothing was changed
        stop_event = threading.Event()
        with self._lock:
            self._stop_event = stop_event
            self._position = 0
            self._ring.clear()
            self._gate.reset()
            self._reset_segment()
            self._speech_run = 0
            self._skip_samples = 0
            self._state = WakeState.ARMED
            thread = threading.Thread(
                target=self._run, args=(stop_event,), name="luna-wake", daemon=True
            )
            self._thread = thread
        self._detector.reset()
        thread.start()
        self._publish(WakeStateChanged(WakeState.ARMED))

    def stop(self) -> None:
        """Close the microphone, close the tap, clear audio, join the thread. Idempotent."""
        with self._lock:
            previous = self._state
            self._state = WakeState.OFF
            self._stop_event.set()
            tap, self._tap = self._tap, None
            thread, self._thread = self._thread, None
            self._ring.clear()
            self._gate.reset()
            self._pending.clear()
        if tap is not None:
            tap.close()
        self._safe_mic_stop()
        stuck = False
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_TIMEOUT_S)
            stuck = thread.is_alive()
            if stuck:
                log.warning("luna-wake did not stop within %.0f s", JOIN_TIMEOUT_S)
        if not stuck:  # never touch the detector while the thread may still be using it
            self._reset_segment()
            self._safe_detector_reset()
        if previous is not WakeState.OFF:
            self._publish(WakeStateChanged(WakeState.OFF))

    def close(self) -> None:
        """``stop()`` and release the microphone and the detector for good."""
        with self._lock:
            self._closed = True
        self.stop()
        try:
            self._mic.close()
        except Exception:
            log.warning("closing the microphone failed", exc_info=True)
        try:
            self._detector.close()
        except Exception:
            log.warning("closing the wake detector failed", exc_info=True)

    # ------------------------------------------------------------------ LiveAudio

    def open_tap(self, preroll_ms: int, *, skip_ms: int = 0) -> LiveSource:
        """A ``LiveSource`` continuing the live stream (previous tap, if any, is closed).

        ``lead_in`` is the last ``preroll_ms`` of the ring; ``skip_ms`` drops that much of the
        following live audio (rounded up to whole frames), e.g. the echo of a reply that was
        just cut off. Raises ``AudioError`` if the listener is not running.
        """
        if preroll_ms < 0 or skip_ms < 0:
            raise ValueError("preroll_ms and skip_ms must not be negative")
        with self._lock:
            if self._state not in (WakeState.ARMED, WakeState.HELD):
                raise AudioError("wake listener is not running")
            snapshot = self._ring.snapshot()
            want = _ms_to_samples(preroll_ms) * SAMPLE_WIDTH_BYTES
            lead_in = snapshot[len(snapshot) - min(want, len(snapshot)) :]
            self._replace_tap(FrameTap())
            self._skip_samples = _ms_to_samples(skip_ms)
            assert self._tap is not None
            return LiveSource(self._tap, lead_in, self.noise_floor)

    def set_hold(self, reason: str, active: bool) -> None:
        """Mute (or unmute) the detector for ``reason``; idempotent and thread-safe.

        ``speaking`` also keeps it muted ``speaking_tail_ms`` after the release; with
        ``mute_while_speaking = false`` that hold is ignored (headset use).
        """
        if reason == SPEAKING_HOLD and not self._conf.mute_while_speaking:
            return
        tail_s = self._conf.speaking_tail_ms / 1000 if reason == SPEAKING_HOLD else 0.0
        with self._lock:
            self._gate.set_hold(reason, active, self._clock(), tail_s)

    def open_window(self, seconds: float) -> None:
        """Follow-up window: after re-arming, speech alone starts a turn for ``seconds``.

        ``0`` closes it. While a hold is active the window waits (half-duplex wins).
        """
        events: list[WakeStateChanged] = []
        with self._lock:
            self._gate.open_window(seconds, self._clock())
            if seconds > 0 and self._state is WakeState.ARMED:
                events.append(WakeStateChanged(WakeState.ARMED, "follow-up"))
        self._publish_all(events)

    # ------------------------------------------------------------------ the luna-wake thread

    def _run(self, stop_event: threading.Event) -> None:
        last_frame_at = self._clock()
        while not stop_event.is_set():
            try:
                frame = self._mic.read(self._read_timeout_s)
            except Exception as e:
                if not stop_event.is_set():
                    self._fail(f"microphone error: {e}", stop_event)
                return
            if frame is None:
                if stop_event.is_set():
                    return
                if self._clock() - last_frame_at > self._stall_s:
                    self._fail("microphone delivered no audio", stop_event)
                    return
                stop_event.wait(0.005)  # a source that returns at once must not spin
                continue
            try:
                self._handle_frame(frame, stop_event)
            except _DetectorFailed as e:
                self._fail(str(e), stop_event)
                return
            except Exception:
                log.exception("wake listener crashed")
                self._fail("wake listener crashed", stop_event)
                return
            # after the handler: a slow on_wake must not look like a silent microphone
            last_frame_at = self._clock()

    def _handle_frame(self, frame: bytes, stop_event: threading.Event) -> None:
        speech = self._vad.is_speech(frame)  # also while held: keeps the noise floor current
        samples = len(frame) // SAMPLE_WIDTH_BYTES
        events: list[WakeStateChanged] = []
        trigger: WakeTrigger | None = None
        with self._lock:
            if stop_event.is_set() or self._state not in (WakeState.ARMED, WakeState.HELD):
                return
            self._position += samples
            self._ring.append(frame)
            self._push_tap(frame, samples)
            now = self._clock()
            self._refresh_state(now, events)
            armed = self._state is WakeState.ARMED
            if armed:
                trigger = self._check_window(speech, samples, now, events)
            else:
                self._speech_run = 0
        self._flush_reset()
        self._publish_all(events)
        if trigger is not None:
            self._deliver(trigger)
            return
        if not armed:
            return
        event = self._feed_detector(frame, speech, samples)
        if event is not None:
            self._on_keyword(event, stop_event)

    def _refresh_state(self, now: float, events: list[WakeStateChanged]) -> None:
        """Apply hold / tail / cooldown changes (lock held, luna-wake thread)."""
        gate = self._gate
        reason = gate.hold_reason(now)
        if self._state is WakeState.ARMED and reason is not None:
            self._state = WakeState.HELD
            self._reset_segment()
            self._need_reset = True
            events.append(WakeStateChanged(WakeState.HELD, reason))
        elif self._state is WakeState.HELD and reason is None:
            self._state = WakeState.ARMED
            self._reset_segment()
            self._need_reset = True
            self._ring.clear()  # the assistant's own voice must not complete a keyword later
            gate.update_window(now)
            events.append(
                WakeStateChanged(WakeState.ARMED, "follow-up" if gate.window_open(now) else None)
            )
        if self._state is WakeState.ARMED and gate.poll_window_expired(now):
            events.append(WakeStateChanged(WakeState.ARMED))

    def _check_window(
        self, speech: bool, samples: int, now: float, events: list[WakeStateChanged]
    ) -> WakeTrigger | None:
        if not self._gate.window_open(now):
            self._speech_run = 0
            return None
        self._speech_run = self._speech_run + samples if speech else 0
        if self._speech_run * 1000 < self._voice.min_speech_ms * SAMPLE_RATE:
            return None
        cut = self._position - self._speech_run  # speech onset
        self._speech_run = 0
        return self._make_trigger(cut, None, self._voice.preroll_ms, now, events)

    def _feed_detector(self, frame: bytes, speech: bool, samples: int) -> WakeEvent | None:
        """VAD gate: the detector only sees audio inside speech segments (luna-wake only)."""
        if not self._seg_open:
            if not speech:
                return None
            snapshot = self._ring.snapshot()
            take = min(len(snapshot), _ms_to_samples(GATE_PREROLL_MS) * 2 + len(frame))
            take -= take % SAMPLE_WIDTH_BYTES
            data = snapshot[len(snapshot) - take :]
            self._seg_open = True
            self._seg_base = self._position - take // SAMPLE_WIDTH_BYTES
            self._seg_silence = 0
            self._seg_samples = take // SAMPLE_WIDTH_BYTES
            self._fed = 0
            self._pending.clear()
        else:
            data = frame
            self._seg_samples += samples
        self._seg_silence = 0 if speech else self._seg_silence + samples
        event = self._run_detector(data)
        if event is not None:
            return event
        if (
            self._seg_silence * 1000 >= SEGMENT_SILENCE_MS * SAMPLE_RATE
            or self._seg_samples * 1000 >= SEGMENT_MAX_MS * SAMPLE_RATE
        ):
            self._reset_segment()
            self._safe_detector_reset()
        return None

    def _run_detector(self, data: bytes) -> WakeEvent | None:
        """Re-chunk ``data`` to the detector's frame size and feed it."""
        self._pending += data
        size = self._det_bytes
        while len(self._pending) >= size:
            chunk = bytes(self._pending[:size])
            del self._pending[:size]
            self._fed += size // SAMPLE_WIDTH_BYTES
            try:
                event = self._detector.process(chunk)
            except Exception as e:
                log.exception("wake detector failed")
                raise _DetectorFailed(f"wake word detector failed: {type(e).__name__}") from e
            if event is not None:
                return event
        return None

    def _on_keyword(self, event: WakeEvent, stop_event: threading.Event) -> None:
        events: list[WakeStateChanged] = []
        with self._lock:
            now = self._clock()
            if (
                stop_event.is_set()
                or self._state is not WakeState.ARMED
                or self._gate.hold_reason(now) is not None
            ):
                return  # a hold arrived while the detector was decoding: ignore the hit
            if event.end_sample is not None:
                end = event.end_sample
            else:
                end = max(0, self._fed - _ms_to_samples(FALLBACK_LAG_MS))
            cut = self._seg_base + end
            self._gate.start_cooldown(now, self._conf.cooldown_s)
            self._state = WakeState.HELD
            self._reset_segment()
            self._need_reset = True
            events.append(WakeStateChanged(WakeState.HELD, "cooldown"))
            trigger = self._make_trigger(cut, event, LEAD_IN_MS, now, events)
        self._flush_reset()
        self._publish_all(events)
        self._deliver(trigger)

    # ------------------------------------------------------------------ helpers

    def _make_trigger(
        self,
        cut: int,
        event: WakeEvent | None,
        lead_ms: int,
        now: float,
        events: list[WakeStateChanged],
    ) -> WakeTrigger:
        """Lock held. Split the ring at ``cut`` into lead_in + tap backlog."""
        snapshot = self._ring.snapshot()
        total = len(snapshot) // SAMPLE_WIDTH_BYTES
        ring_start = self._position - total
        cut = min(max(cut, ring_start), self._position)
        at = (cut - ring_start) * SAMPLE_WIDTH_BYTES
        lead, backlog = snapshot[:at], snapshot[at:]
        # Whole frames only: the part that does not fill one joins the lead-in.
        remainder = len(backlog) % FRAME_BYTES
        lead, backlog = lead + backlog[:remainder], backlog[remainder:]
        lead = lead[len(lead) - min(len(lead), _ms_to_samples(lead_ms) * 2) :]
        frames = [backlog[i : i + FRAME_BYTES] for i in range(0, len(backlog), FRAME_BYTES)]
        tap = FrameTap(frames)
        self._replace_tap(tap)
        self._skip_samples = 0
        if event is None:  # follow-up window: it is consumed, the cooldown protects the turn
            self._gate.start_cooldown(now, self._conf.cooldown_s)
            if self._state is WakeState.ARMED:
                self._state = WakeState.HELD
                self._reset_segment()
                self._need_reset = True
                events.append(WakeStateChanged(WakeState.HELD, "cooldown"))
        return WakeTrigger(LiveSource(tap, lead, self.noise_floor), event, now)

    def _deliver(self, trigger: WakeTrigger) -> None:
        """Call ``on_wake`` without any lock; close the tap if the turn was refused.

        The follow-up window is consumed by a trigger, but given back if the turn is refused.
        """
        with self._lock:
            saved_window = self._gate.window_deadline
            self._gate.open_window(0, self._clock())
        try:
            accepted = bool(self._on_wake(trigger))
        except Exception:
            log.exception("on_wake callback failed")
            accepted = False
        if not accepted:
            tap = trigger.source.frames
            with self._lock:
                self._gate.restore_window(saved_window)
                if self._tap is tap:
                    self._tap = None
            if isinstance(tap, FrameTap):
                tap.close()

    def _replace_tap(self, tap: FrameTap) -> None:
        old, self._tap = self._tap, tap
        if old is not None:
            old.close()

    def _push_tap(self, frame: bytes, samples: int) -> None:
        tap = self._tap
        if tap is None:
            return
        if tap.closed:
            self._tap = None
        elif self._skip_samples > 0:
            self._skip_samples -= samples
        else:
            tap.push(frame)

    def _fail(self, reason: str, stop_event: threading.Event) -> None:
        with self._lock:
            if stop_event.is_set() or self._state not in (WakeState.ARMED, WakeState.HELD):
                return
            self._state = WakeState.FAILED
            tap, self._tap = self._tap, None
            self._ring.clear()
            self._gate.reset()
        log.warning("wake listener failed: %s", reason)
        if tap is not None:
            tap.close(AudioError(reason))
        self._safe_mic_stop()
        self._reset_segment()
        self._safe_detector_reset()
        self._publish(WakeStateChanged(WakeState.FAILED, reason))

    def _reset_segment(self) -> None:
        self._seg_open = False
        self._seg_silence = 0
        self._seg_samples = 0
        self._fed = 0
        self._pending.clear()

    def _flush_reset(self) -> None:
        """Run a requested ``detector.reset()`` (luna-wake thread, lock not held)."""
        if self._need_reset:
            self._need_reset = False
            self._safe_detector_reset()

    def _safe_detector_reset(self) -> None:
        try:
            self._detector.reset()
        except Exception:
            log.warning("resetting the wake detector failed", exc_info=True)

    def _safe_mic_stop(self) -> None:
        try:
            self._mic.stop()
        except Exception:
            log.warning("stopping the microphone failed", exc_info=True)

    def _publish(self, event: WakeStateChanged) -> None:
        self._bus.publish(event)  # the bus delivers any event object

    def _publish_all(self, events: list[WakeStateChanged]) -> None:
        for event in events:
            self._publish(event)


class _DetectorFailed(Exception):
    pass


def _ms_to_samples(ms: int) -> int:
    return SAMPLE_RATE * ms // 1000
