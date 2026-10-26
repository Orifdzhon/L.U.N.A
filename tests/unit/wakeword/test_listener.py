"""WakeListener with fakes: no microphone, no Vosk, no model (fake clock where time matters)."""

import threading
import time

import pytest

from fakes import FakeClock, FakeLiveMic, FakeVAD, FakeWakeWord
from luna.audio.capture import FrameTap
from luna.config.settings import VoiceSettings, WakeWordSettings
from luna.core.errors import AudioError
from luna.core.events import EventBus
from luna.wakeword.listener import (
    WakeGate,
    WakeListener,
    WakeState,
    WakeStateChanged,
    WakeTrigger,
)

FRAME_SAMPLES = 480
SILENCE = bytes(960)


def speech(i: int) -> bytes:
    """A distinguishable speech frame (non-zero bytes), 30 ms."""
    return bytes([i % 250 + 1]) * 960


def wake_frame(i: int) -> bytes:
    return b"WAKE" + bytes([i % 250 + 1]) * 956


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


def drain(tap: FrameTap) -> list[bytes]:
    frames = []
    while (frame := tap.read(0.0)) is not None:
        frames.append(frame)
    return frames


class Harness:
    def __init__(self, *, detector=None, accept=True, conf=None, on_wake=None, stall_s=1000.0):
        self.mic = FakeLiveMic()
        self.detector = detector or FakeWakeWord()
        self.vad = FakeVAD()
        self.clock = FakeClock()
        self.bus = EventBus()
        self.sub = self.bus.subscribe()
        self.triggers: list[WakeTrigger] = []
        self.accept = accept
        self.custom_on_wake = on_wake
        self.listener = WakeListener(
            self.mic,
            self.detector,
            self._make_vad,
            conf or WakeWordSettings(cooldown_s=2.0, speaking_tail_ms=400),
            VoiceSettings(),
            self.bus,
            on_wake=self._on_wake,
            clock=self.clock,
            stall_s=stall_s,
            read_timeout_s=0.01,
        )
        self.fed = 0

    def _make_vad(self):
        self.vad_made = getattr(self, "vad_made", 0) + 1
        return self.vad

    def _on_wake(self, trigger):
        self.triggers.append(trigger)
        if self.custom_on_wake is not None:
            return self.custom_on_wake(self, trigger)
        return self.accept

    def feed(self, *frames):
        self.mic.feed(*frames)
        self.fed += len(frames)
        assert wait_until(lambda: self.listener.position == self.fed * FRAME_SAMPLES), (
            "listener did not consume the frames"
        )

    def states(self):
        out = []
        while not self.sub.queue.empty():
            event = self.sub.queue.get_nowait()
            if isinstance(event, WakeStateChanged):
                out.append((event.state, event.detail))
        return out

    def stop(self):
        self.listener.close()


@pytest.fixture
def make():
    made = []

    def factory(**kw):
        h = Harness(**kw)
        made.append(h)
        return h

    yield factory
    for h in made:
        h.stop()


def started(h):
    h.listener.start()
    return h


def test_start_opens_mic_and_arms(make):
    h = make()
    assert h.listener.state is WakeState.OFF
    assert not h.mic.started
    h.listener.start()
    assert h.mic.started and h.listener.state is WakeState.ARMED
    assert h.states() == [(WakeState.ARMED, None)]
    h.listener.start()  # idempotent
    assert h.mic.start_calls == 1
    assert sum(t.name == "luna-wake" for t in threading.enumerate()) >= 1


def test_start_failure_propagates_and_stays_off(make):
    h = make()
    h.mic.error = AudioError("no microphone")
    with pytest.raises(AudioError):
        h.listener.start()
    assert h.listener.state is WakeState.OFF


def test_silence_never_reaches_the_detector(make):
    h = started(make())
    h.feed(*[SILENCE] * 40)
    assert h.detector.frames == []
    assert h.vad.calls == 40


def test_wake_frame_in_silence_triggers_with_preroll(make):
    h = started(make())
    h.feed(*[SILENCE] * 12)
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)
    trigger = h.triggers[0]
    # 300 ms pre-roll (10 frames) + the speech frame itself reached the detector, nothing before
    assert len(h.detector.frames) == 11
    assert trigger.event is not None and trigger.event.keyword == "luna"
    assert trigger.detected_at == 0.0
    # the keyword ended with the last frame: no backlog, lead_in = last second (33 frames)
    assert drain(trigger.source.frames) == []
    assert trigger.source.lead_in.endswith(wake_frame(0))
    assert len(trigger.source.lead_in) == 13 * 960  # all 13 frames heard so far
    assert trigger.source.noise_floor == h.vad.noise_floor
    assert (WakeState.HELD, "cooldown") in h.states()


def test_lead_in_is_capped_at_one_second(make):
    h = started(make())
    h.feed(*[SILENCE] * 60)  # the ring (2 s) holds more than a second
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)
    assert len(h.triggers[0].source.lead_in) == 32000


def test_cut_splits_ring_into_lead_in_and_backlog_and_rechunks(make):
    # detector wants 960-sample chunks (2 mic frames); the keyword "ended" 480 samples before
    # the end of the second chunk, i.e. at the end of the 3rd mic frame.
    det = FakeWakeWord(trigger_at={1}, frame_samples=960, end_lag_samples=480, marker=False)
    h = started(make(detector=det))
    frames = [speech(i) for i in range(7)]
    h.feed(*frames[:4])
    assert wait_until(lambda: len(h.triggers) == 1)
    trigger = h.triggers[0]
    assert [len(f) for f in det.frames] == [1920, 1920]
    assert det.frames[0] == frames[0] + frames[1]
    assert trigger.source.lead_in == b"".join(frames[:3])
    assert drain(trigger.source.frames) == [frames[3]]
    # the tap continues with live frames, in order
    h.feed(*frames[4:])
    assert drain(trigger.source.frames) == frames[4:]


def test_cut_inside_a_frame_gives_whole_frame_backlog(make):
    # cut falls in the middle of the last mic frame: the partial frame joins the lead-in
    det = FakeWakeWord(trigger_at={0}, frame_samples=960, end_lag_samples=240, marker=False)
    h = started(make(detector=det))
    frames = [speech(i) for i in range(2)]
    h.feed(*frames)
    assert wait_until(lambda: len(h.triggers) == 1)
    trigger = h.triggers[0]
    assert trigger.source.lead_in == b"".join(frames)
    assert drain(trigger.source.frames) == []


def test_missing_end_sample_falls_back_to_fed_minus_250ms(make):
    det = FakeWakeWord(trigger_at={9}, report_end=False, marker=False)
    h = started(make(detector=det))
    frames = [speech(i) for i in range(10)]
    h.feed(*frames)
    assert wait_until(lambda: len(h.triggers) == 1)
    # fed 10 frames = 4800 samples; cut = 4800 - 4000 = 800 -> lead_in 800 samples, rest backlog
    trigger = h.triggers[0]
    backlog = drain(trigger.source.frames)
    assert len(backlog) == 8  # (4800 - 800) samples = 8 whole frames
    assert trigger.source.lead_in == b"".join(frames[:2])  # 800 samples + the partial frame
    assert b"".join(backlog) == b"".join(frames[2:])


def test_segment_closes_after_600ms_silence_and_resets_detector(make):
    h = started(make())
    base = h.detector.reset_count
    h.feed(speech(0), speech(1))
    assert len(h.detector.frames) == 2
    h.feed(*[SILENCE] * 19)  # 570 ms: the segment is still open, silence is fed
    assert len(h.detector.frames) == 21
    assert h.detector.reset_count == base
    h.feed(SILENCE)  # 600 ms
    assert h.detector.reset_count == base + 1
    h.feed(*[SILENCE] * 5)
    assert len(h.detector.frames) == 22  # gated again


def test_segment_is_forced_closed_after_10_seconds(make):
    h = started(make())
    base = h.detector.reset_count
    h.feed(*[speech(i) for i in range(340)])
    assert h.detector.reset_count >= base + 1


def test_ring_buffer_is_bounded(make):
    h = started(make())
    h.feed(*[SILENCE] * 300)  # 9 s of audio
    assert h.listener.buffered_bytes <= 2000 * 32


def test_refused_wake_closes_tap_and_cooldown_blocks_next(make):
    h = started(make(accept=False))
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)
    assert h.triggers[0].source.frames.closed
    h.feed(wake_frame(1), wake_frame(2))
    assert len(h.triggers) == 1  # cooldown
    h.clock.advance(2.1)
    h.feed(speech(3))  # re-arms
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)
    h.feed(wake_frame(4))
    assert wait_until(lambda: len(h.triggers) == 2)


def test_rearm_resets_detector_and_clears_ring(make):
    h = started(make())
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)
    h.feed(*[speech(i) for i in range(5)])
    before = h.detector.reset_count
    h.clock.advance(2.1)
    h.feed(SILENCE)
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)
    assert h.detector.reset_count > before
    assert h.listener.buffered_bytes == 0


def test_hold_suppresses_detection_and_release_resets(make):
    h = started(make())
    h.listener.set_hold("speaking", True)
    h.feed(wake_frame(0), wake_frame(1))
    assert h.triggers == [] and h.detector.frames == []
    assert wait_until(lambda: h.listener.state is WakeState.HELD)
    h.listener.set_hold("speaking", True)  # idempotent
    h.listener.set_hold("speaking", False)
    resets = h.detector.reset_count
    h.feed(wake_frame(2))  # inside the 400 ms tail
    assert h.triggers == [] and h.listener.state is WakeState.HELD
    h.clock.advance(0.5)
    h.feed(SILENCE)
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)
    assert h.detector.reset_count > resets
    assert h.listener.buffered_bytes == 0  # the reply's audio is gone
    h.feed(wake_frame(3))
    assert wait_until(lambda: len(h.triggers) == 1)
    assert (WakeState.HELD, "speaking") in h.states()


def test_turn_holds_are_independent(make):
    h = started(make())
    h.listener.set_hold("turn:1", True)
    h.listener.set_hold("turn:2", True)
    h.listener.set_hold("turn:1", False)  # a superseded turn must not unmute the new one
    h.clock.advance(5)
    h.feed(wake_frame(0))
    assert h.triggers == []
    h.listener.set_hold("turn:2", False)
    h.feed(SILENCE)
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)


def test_mute_while_speaking_false_ignores_speaking_hold(make):
    h = started(make(conf=WakeWordSettings(mute_while_speaking=False)))
    h.listener.set_hold("speaking", True)
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)


def test_open_tap_lead_in_skip_and_replacement(make):
    h = started(make())
    frames = [speech(i) for i in range(6)]
    h.feed(*frames)
    src = h.listener.open_tap(90)  # 3 frames
    assert src.lead_in == b"".join(frames[3:])
    assert src.noise_floor == h.vad.noise_floor
    h.feed(speech(10))
    assert drain(src.frames) == [speech(10)]
    second = h.listener.open_tap(0, skip_ms=60)
    assert src.frames.closed and not second.frames.closed
    assert second.lead_in == b""
    h.feed(speech(11), speech(12), speech(13))
    assert drain(second.frames) == [speech(13)]  # first two frames (60 ms) skipped


def test_open_tap_requires_running_listener(make):
    h = make()
    with pytest.raises(AudioError):
        h.listener.open_tap(300)


def test_mic_failure_fails_listener_and_closes_tap_with_error(make):
    h = started(make())
    src = h.listener.open_tap(0)
    h.mic.fail(AudioError("unplugged"))
    assert wait_until(lambda: h.listener.state is WakeState.FAILED)
    assert h.mic.stop_calls >= 1
    with pytest.raises(AudioError):
        src.frames.read(0.1)
    assert (WakeState.FAILED, "microphone error: unplugged") in h.states()
    assert h.listener.buffered_bytes == 0
    h.listener.start()  # retry works
    assert h.listener.state is WakeState.ARMED


def test_stalled_microphone_fails_listener(make):
    h = started(make(stall_s=2.0))
    h.feed(speech(0))
    h.clock.advance(2.5)
    assert wait_until(lambda: h.listener.state is WakeState.FAILED)


def test_detector_exception_fails_listener(make):
    h = started(make(detector=FakeWakeWord(error=RuntimeError("boom"))))
    h.mic.feed(speech(0))
    assert wait_until(lambda: h.listener.state is WakeState.FAILED)
    assert (WakeState.FAILED, "wake word detector failed: RuntimeError") in h.states()


def test_stop_joins_thread_closes_mic_and_clears(make):
    h = started(make())
    src = h.listener.open_tap(0)
    h.feed(speech(0), speech(1))
    h.listener.stop()
    assert h.listener.state is WakeState.OFF
    assert not h.mic.started and h.mic.stop_calls >= 1
    assert src.frames.closed
    assert h.listener.buffered_bytes == 0
    assert not any(t.name == "luna-wake" and t.is_alive() for t in threading.enumerate())
    h.listener.stop()  # idempotent
    assert h.states()[-1] == (WakeState.OFF, None)
    h.listener.start()  # can be started again
    assert h.listener.state is WakeState.ARMED


def test_close_releases_everything(make):
    h = started(make())
    h.listener.close()
    assert h.mic.closed and h.detector.closed
    assert not any(t.name == "luna-wake" and t.is_alive() for t in threading.enumerate())
    with pytest.raises(AudioError):
        h.listener.start()


def test_on_wake_may_call_back_into_the_listener(make):
    seen = {}

    def callback(h, trigger):
        h.listener.set_hold("turn:1", True)
        seen["state"] = h.listener.state
        seen["tap"] = h.listener.open_tap(300)
        h.listener.open_window(0)
        return True

    h = started(make(on_wake=callback))
    h.feed(wake_frame(0))
    assert wait_until(lambda: "tap" in seen)
    h.feed(speech(1))  # the loop is alive: it keeps reading after the callback
    assert drain(seen["tap"].frames) == [speech(1)]
    assert h.listener.state is WakeState.HELD


def test_on_wake_may_stop_the_listener(make):
    h = started(make(on_wake=lambda h, t: (h.listener.stop(), True)[1]))
    h.mic.feed(wake_frame(0))
    assert wait_until(lambda: h.listener.state is WakeState.OFF)
    assert wait_until(lambda: not any(t.name == "luna-wake" for t in threading.enumerate()))


def test_on_wake_exception_is_contained(make):
    def boom(h, t):
        raise RuntimeError("x")

    h = started(make(on_wake=boom))
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)
    assert h.triggers[0].source.frames.closed
    h.feed(SILENCE)
    assert h.listener.state is not WakeState.FAILED


# --- follow-up window ------------------------------------------------------------------


def test_follow_up_window_fires_on_speech_onset(make):
    h = started(make(accept=True))
    h.listener.open_window(6.0)
    frames = [speech(i) for i in range(10)]
    h.feed(*frames[:6])  # 180 ms: shorter than min_speech_ms (200)
    assert h.triggers == []
    h.feed(*frames[6:8])  # 240 ms
    assert wait_until(lambda: len(h.triggers) == 1)
    trigger = h.triggers[0]
    assert trigger.event is None
    # cut = speech onset (frame 0 of this stream of 8): everything heard is backlog
    assert drain(trigger.source.frames) == frames[:8]
    assert trigger.source.lead_in == b""


def test_follow_up_window_expires(make):
    h = started(make())
    h.listener.open_window(6.0)
    h.states()
    h.clock.advance(7)
    h.feed(speech(0), speech(1), speech(2), speech(3), speech(4), speech(5), speech(6), speech(7))
    assert h.triggers == []
    assert (WakeState.ARMED, None) in h.states()


def test_follow_up_window_waits_for_holds(make):
    h = started(make())
    h.listener.set_hold("turn:1", True)
    h.listener.open_window(6.0)
    h.clock.advance(10)
    h.feed(SILENCE)
    h.listener.set_hold("turn:1", False)
    h.feed(SILENCE)  # re-armed now: window starts here
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)
    h.feed(*[speech(i) for i in range(8)])
    assert wait_until(lambda: len(h.triggers) == 1)


# --- WakeGate (pure) -------------------------------------------------------------------


def test_gate_holds_tail_cooldown():
    g = WakeGate()
    assert g.hold_reason(0) is None
    g.set_hold("speaking", True, 0, 0.4)
    assert g.hold_reason(1) == "speaking"
    g.set_hold("turn:1", True, 1)
    assert g.hold_reason(1) == "speaking"  # sorted: "speaking" < "turn:1"
    g.set_hold("turn:1", False, 2)
    assert g.hold_reason(2.1) == "speaking"
    g.set_hold("speaking", False, 3, 0.4)
    assert g.hold_reason(3.3) == "tail"
    assert g.hold_reason(3.5) is None
    g.start_cooldown(10, 2.0)
    assert g.hold_reason(11) == "cooldown" and g.hold_reason(12.1) is None
    g.set_hold("never-set", False, 20)  # releasing an unknown hold is a no-op
    assert g.hold_reason(20) is None


def test_gate_window_lifecycle():
    g = WakeGate()
    g.open_window(5, 0)
    assert g.window_open(1) and not g.window_open(5)
    assert g.poll_window_expired(5) and not g.poll_window_expired(6)
    g.set_hold("turn:1", True, 10)
    g.open_window(5, 10)
    assert g.window_pending() and not g.window_open(11)
    g.set_hold("turn:1", False, 12)
    g.update_window(12)
    assert g.window_open(13) and not g.window_pending()
    g.open_window(0, 13)
    assert not g.window_open(13)


# --- review fixes ----------------------------------------------------------------------


def test_listener_builds_its_own_vad_once(make):
    h = make()
    assert h.vad_made == 1


def test_slow_on_wake_does_not_look_like_a_dead_microphone(make):
    def slow(h, trigger):
        h.clock.advance(5.0)  # on_wake blocked for 5 s on the fake clock
        return True

    h = started(make(on_wake=slow, stall_s=2.0))
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)
    time.sleep(0.1)  # several idle reads happen after the callback
    assert h.listener.state is not WakeState.FAILED


def test_detector_reset_never_runs_under_the_listener_lock(make):
    holder = {}

    class Probe(FakeWakeWord):
        locked_during_reset = False

        def reset(self):
            listener = holder.get("listener")
            if listener is not None:
                Probe.locked_during_reset |= listener._lock.locked()
            super().reset()

    h = make(detector=Probe())
    holder["listener"] = h.listener
    h.listener.start()
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)
    h.listener.set_hold("turn:1", True)
    h.feed(SILENCE)
    h.listener.set_hold("turn:1", False)
    h.clock.advance(3)
    h.feed(SILENCE)
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)
    assert h.detector.reset_count >= 3
    assert not Probe.locked_during_reset


def test_restart_after_failure_works_end_to_end(make):
    h = started(make())
    h.mic.fail(AudioError("unplugged"))
    assert wait_until(lambda: h.listener.state is WakeState.FAILED)
    h.listener.start()
    assert h.listener.state is WakeState.ARMED and h.mic.start_calls == 2
    assert h.listener.position == 0
    h.fed = 0
    h.feed(wake_frame(0))
    assert wait_until(lambda: len(h.triggers) == 1)


def test_cut_before_ring_start_is_clamped(make):
    # the detector reports a keyword end 3 s back, older than the 2 s ring
    det = FakeWakeWord(trigger_at={9}, frame_samples=4800, end_lag_samples=45000, marker=False)
    h = started(make(detector=det))
    h.feed(*[speech(i) for i in range(100)])
    assert wait_until(lambda: len(h.triggers) == 1)
    trigger = h.triggers[0]
    backlog = drain(trigger.source.frames)
    # the whole 2 s ring is after the cut; the part that is not a whole frame joins the lead-in
    assert len(trigger.source.lead_in) == 640
    assert len(b"".join(backlog)) == 64000 - 640
    assert all(len(f) == 960 for f in backlog)


def test_skip_ms_rounds_up_to_whole_frames(make):
    h = started(make())
    h.feed(speech(0))
    src = h.listener.open_tap(0, skip_ms=40)  # 1.33 frames -> 2 frames are dropped
    h.feed(speech(1), speech(2), speech(3))
    assert drain(src.frames) == [speech(3)]


def test_refused_follow_up_keeps_the_window(make):
    h = started(make(accept=False))
    h.listener.open_window(30.0)
    h.feed(*[speech(i) for i in range(8)])
    assert wait_until(lambda: len(h.triggers) == 1)
    assert h.triggers[0].event is None
    h.clock.advance(3.0)  # cooldown over, window (30 s) still running
    h.feed(SILENCE)
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)
    h.feed(*[speech(i) for i in range(8)])
    assert wait_until(lambda: len(h.triggers) == 2)


def test_accepted_trigger_consumes_the_window(make):
    h = started(make(accept=True))
    h.listener.open_window(30.0)
    h.feed(*[speech(i) for i in range(8)])
    assert wait_until(lambda: len(h.triggers) == 1)
    h.clock.advance(3.0)
    h.feed(SILENCE)
    assert wait_until(lambda: h.listener.state is WakeState.ARMED)
    h.feed(*[speech(i) for i in range(8)])
    assert len(h.triggers) == 1
