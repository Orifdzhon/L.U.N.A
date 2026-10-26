"""VoiceFrontEnd wake-triggered turns with local verification (fakes only: no mic, no model)."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import pytest

from fakes import FakeHotkeyListener, FakeSTT, FakeVerifier
from luna.audio.capture import FrameTap, LiveSource
from luna.audio.recorder import Utterance
from luna.config.settings import STTSettings, VoiceSettings, WakeWordSettings
from luna.core.errors import AudioError, ProviderUnavailable
from luna.core.events import CLOSED, EventBus, Status, StatusChanged
from luna.core.models import Reply, Request
from luna.stt.base import Transcript
from luna.voice.frontend import VoiceFrontEnd, VoiceState, _TeeTap
from luna.wakeword.base import WakeEvent
from luna.wakeword.listener import WakeTrigger
from luna.wakeword.verify import VerifyResult

FRAME = b"\x01\x00" * 480  # 30 ms
LEAD_IN = b"\x02\x00" * 16000  # 1 s
PCM = b"\x01\x00" * 1600
SPOKEN = Utterance(PCM, 16000, 0.1, "silence")
NO_SPEECH = Utterance(b"", 16000, 0.0, "no_speech")
CANCELLED = Utterance(b"", 16000, 0.0, "cancelled")
LEAD_BYTES = 700 * 32  # 700 ms of 16 kHz 16-bit audio
WINDOW_BYTES = 2 * LEAD_BYTES


def ok(text: str = "open the browser") -> Transcript:
    return Transcript(text, "ru", provider="fake")


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.002)
    raise AssertionError("condition not reached in time")


class FakeLive:
    """``LiveAudio``: records holds and taps; ``open_tap`` returns a tap with queued frames."""

    def __init__(self) -> None:
        self.holds: set[str] = set()
        self.hold_log: list[tuple[str, bool]] = []
        self.taps: list[tuple[int, int]] = []  # (preroll_ms, skip_ms)
        self.tap_error: Exception | None = None
        self.windows: list[float] = []
        self._lock = threading.Lock()

    def set_hold(self, reason: str, active: bool) -> None:
        with self._lock:
            self.hold_log.append((reason, active))
            (self.holds.add if active else self.holds.discard)(reason)

    def open_tap(self, preroll_ms: int, *, skip_ms: int = 0) -> LiveSource:
        if self.tap_error is not None:
            raise self.tap_error
        self.taps.append((preroll_ms, skip_ms))
        return LiveSource(FrameTap([FRAME] * 30), LEAD_IN[: preroll_ms * 32], 0.001)

    def open_window(self, seconds: float) -> None:
        self.windows.append(seconds)

    def trigger(self, *, frames: int = 30, follow_up: bool = False) -> WakeTrigger:
        tap = FrameTap([FRAME] * frames)
        event = None if follow_up else WakeEvent("luna", 0.9, 0)
        return WakeTrigger(LiveSource(tap, LEAD_IN, 0.001), event, 0.0)


class LiveRecorder:
    """Script items (one per ``record`` call): "frames" reads 30 frames then returns SPOKEN,
    "block" waits for cancel/stop, an ``Utterance`` is returned at once, an exception is raised.
    Reads from ``live.frames`` like the real Recorder and closes it on return."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.entered = threading.Event()

    def record(self, cancel, *, stop=None, live=None, no_speech_s=None) -> Utterance:
        self.calls.append({"live": live, "no_speech_s": no_speech_s, "cancel": cancel})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            item = self.script.pop(0) if self.script else "frames"
            if isinstance(item, Exception):
                raise item
            if isinstance(item, Utterance):
                return item
            seen = 0
            while not cancel.is_set():
                if stop is not None and stop.is_set():
                    return Utterance(PCM, 16000, 0.1, "stopped")
                frame = live.frames.read(0.005) if live is not None else None
                if frame is None:
                    if live is not None and getattr(live.frames, "closed", False):
                        return CANCELLED
                    time.sleep(0.002)
                    continue
                seen += 1
                if item == "frames" and seen >= 30:
                    return SPOKEN
            return CANCELLED
        finally:
            self.active -= 1
            if live is not None:
                live.frames.close()


class FakeSpeaker:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.active = False

    def stop(self) -> None:
        self.log.append("stop_speech")


class FakeOrchestrator:
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[Request] = []
        self.error = error

    def handle(self, request: Request, cancel: threading.Event | None = None) -> Reply:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return Reply("answer", "ru", provider="router")


class Harness:
    def __init__(
        self,
        script: list,
        *,
        verifier: FakeVerifier | str | None = "default",
        stt: FakeSTT | None = None,
        wake: WakeWordSettings | None = None,
        orchestrator: FakeOrchestrator | None = None,
        verify_timeout_s: float = 3.0,
        live: bool = True,
    ) -> None:
        self.log: list[str] = []
        self.hotkey = FakeHotkeyListener()
        self.recorder = LiveRecorder(script)
        self.stt = stt or FakeSTT([ok()] * 4)
        self.orch = orchestrator or FakeOrchestrator()
        self.bus = EventBus()
        self.sub = self.bus.subscribe()
        self.speaker = FakeSpeaker(self.log)
        self.live = FakeLive()
        self.replies: list[Reply] = []
        self.beeps = 0
        self.verifier = FakeVerifier() if verifier == "default" else verifier
        self.fe = VoiceFrontEnd(
            self.hotkey,
            self.recorder,  # type: ignore[arg-type]
            self.stt,
            self.orch,
            self.speaker,
            self.bus,
            self._on_reply,
            voice=VoiceSettings(),
            stt_conf=STTSettings(),
            beep=self._beep,
            verify_timeout_s=verify_timeout_s,
        )
        self.fe.start()
        if live:
            self.fe.set_live(
                self.live,
                wake or WakeWordSettings(cue="beep"),
                self.verifier,  # type: ignore[arg-type]
            )

    def _on_reply(self, reply: Reply) -> None:
        self.log.append("on_reply")
        if self.fe._live is not None:
            assert self.live.holds, "the turn hold must still be set during on_reply"
        self.replies.append(reply)

    def _beep(self) -> None:
        self.beeps += 1
        self.log.append("beep")

    def events(self) -> list:
        out = []
        while True:
            try:
                item = self.sub.queue.get_nowait()
            except queue.Empty:
                return out
            if item is not CLOSED:
                out.append(item)

    def statuses(self) -> list[Status]:
        return [e.status for e in self.events() if isinstance(e, StatusChanged)]

    def idle(self) -> None:
        wait_for(lambda: self.fe.state is VoiceState.IDLE)
        wait_for(lambda: not self.live.holds)

    def assert_holds_balanced(self) -> None:
        wait_for(lambda: not self.live.holds)
        on = [n for n, a in self.live.hold_log if a]
        off = [n for n, a in self.live.hold_log if not a]
        assert sorted(on) == sorted(off) and all(n.startswith("turn:") for n in on)


@pytest.fixture
def make():
    made: list[Harness] = []

    def factory(script=(), **kw) -> Harness:
        h = Harness(list(script), **kw)
        made.append(h)
        return h

    yield factory
    for h in made:
        h.fe.close()
        assert all(not t.is_alive() for t in h.fe._threads), "turn thread leaked"


def wake(h: Harness, **kw) -> bool:
    return h.fe.on_wake(h.live.trigger(**kw))


# ---- accept ------------------------------------------------------------------------------


def test_accepted_wake_turn(make, caplog):
    caplog.set_level(logging.INFO, logger="luna.voice.frontend")
    h = make()
    assert wake(h) is True
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.statuses()[0] is Status.LISTENING
    (request,) = h.orch.requests
    assert request.text == "open the browser" and request.source == "voice"
    (window,) = h.verifier.calls
    assert len(window) == WINDOW_BYTES  # 700 ms of lead-in + 700 ms read after the cut
    assert window[:LEAD_BYTES] == LEAD_IN[-LEAD_BYTES:]
    assert window[LEAD_BYTES:] == FRAME * 23 + FRAME[: LEAD_BYTES - 23 * len(FRAME)]
    call = h.recorder.calls[0]
    assert call["live"] is not None and call["no_speech_s"] == 5.0
    # speaker.stop and the cue come after the accept, before the reply
    assert h.log == ["stop_speech", "beep", "on_reply"]
    assert h.live.hold_log[0][1] is True and h.live.hold_log[-1][1] is False
    h.assert_holds_balanced()
    line = next(r.getMessage() for r in caplog.records if "voice turn" in r.getMessage())
    assert "trigger=wake" in line and "verify=accepted" in line and "verify_ms=" in line


def test_hold_is_released_after_on_reply(make):
    h = make()
    seen: list[set[str]] = []
    original = h.fe._on_reply

    def on_reply(reply: Reply) -> None:
        original(reply)
        seen.append(set(h.live.holds))

    h.fe._on_reply = on_reply
    wake(h)
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert seen and all(seen)  # held while the reply was handed to the UI
    assert not h.live.holds


def test_no_verifier_listens_at_once(make, caplog):
    caplog.set_level(logging.INFO, logger="luna.voice.frontend")
    h = make(verifier=None)
    assert wake(h) is True
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.log[0] == "stop_speech"
    assert h.statuses()[0] is Status.LISTENING
    line = next(r.getMessage() for r in caplog.records if "voice turn" in r.getMessage())
    assert "trigger=wake verify=off" in line


def test_cue_off_by_default_does_not_beep(make):
    h = make(wake=WakeWordSettings())
    wake(h)
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.beeps == 0


# ---- reject ------------------------------------------------------------------------------


def test_rejected_wake_is_silent(make, caplog):
    caplog.set_level(logging.INFO, logger="luna.voice.frontend")
    h = make(verifier=FakeVerifier(VerifyResult(False, "no_keyword", "лунный")))
    assert wake(h) is True
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    assert h.statuses() == []  # no LISTENING, no IDLE: nothing was ever shown
    assert h.stt.calls == [] and h.orch.requests == [] and h.replies == []
    assert h.beeps == 0 and "stop_speech" not in h.log
    assert h.fe.state is VoiceState.IDLE
    h.assert_holds_balanced()
    assert any("wake rejected" in r.getMessage() for r in caplog.records)
    assert "лунный" not in caplog.text


def test_after_a_rejection_the_next_wake_is_accepted(make):
    verifier = FakeVerifier(
        [VerifyResult(False, "no_keyword"), VerifyResult(True, "keyword", "луна")]
    )
    h = make(verifier=verifier)
    wake(h)
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    assert wake(h) is True
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert len(h.stt.calls) == 1
    assert h.recorder.max_active == 1


# ---- error / timeout -> fail open --------------------------------------------------------


def test_verifier_exception_fails_open_and_warns_once(make, caplog):
    caplog.set_level(logging.WARNING, logger="luna.voice.frontend")
    h = make(verifier=FakeVerifier(error=RuntimeError("boom")))
    for expected in (1, 2):
        assert wake(h) is True
        wait_for(lambda n=expected: len(h.replies) == n)
        h.idle()
    assert len(h.stt.calls) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_verifier_error_result_fails_open(make, caplog):
    caplog.set_level(logging.INFO, logger="luna.voice.frontend")
    h = make(verifier=FakeVerifier(VerifyResult(True, "error")))
    wake(h)
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.statuses()[0] is Status.LISTENING
    line = next(r.getMessage() for r in caplog.records if "voice turn" in r.getMessage())
    assert "verify=error" in line


def test_verifier_timeout_fails_open(make, caplog):
    caplog.set_level(logging.INFO, logger="luna.voice.frontend")
    h = make(verifier=FakeVerifier(delay=0.4), verify_timeout_s=0.05)
    wake(h)
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.statuses()[0] is Status.LISTENING
    assert len(h.stt.calls) == 1
    assert sum("timed out" in r.getMessage() for r in caplog.records) == 1
    line = next(r.getMessage() for r in caplog.records if "voice turn" in r.getMessage())
    assert "verify=timeout" in line


def test_timeout_publishes_listening_while_still_recording(make):
    h = make(script=["block"], verifier=FakeVerifier(delay=0.4), verify_timeout_s=0.05)
    wake(h)
    wait_for(lambda: Status.LISTENING in h.statuses())
    assert h.fe.state is VoiceState.LISTENING
    h.fe.stop()


# ---- hotkey during pending verification --------------------------------------------------


def test_hotkey_cancels_pending_verification(make):
    gate = threading.Event()

    class SlowVerifier(FakeVerifier):
        def verify(self, pcm16: bytes):
            gate.wait(3)
            return super().verify(pcm16)

    h = make(script=["block", "frames"], verifier=SlowVerifier())
    wake(h)
    wait_for(lambda: h.recorder.entered.is_set())
    assert h.statuses() == []  # unverified: still nothing shown
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    gate.set()  # the late verdict is ignored
    h.idle()
    assert h.recorder.max_active == 1  # the cancelled wake recording ended first
    statuses = h.statuses()
    assert statuses.count(Status.LISTENING) == 1  # only the hotkey turn
    assert len(h.stt.calls) == 1 and len(h.orch.requests) == 1
    h.assert_holds_balanced()


# ---- transcripts -------------------------------------------------------------------------


def test_keyword_only_transcript_ends_silently(make):
    h = make(stt=FakeSTT([ok("Луна.")]))
    wake(h)
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    assert h.orch.requests == [] and h.replies == []
    assert h.statuses()[-1] is Status.IDLE
    h.assert_holds_balanced()


def test_leading_keyword_is_stripped(make):
    h = make(stt=FakeSTT([ok("Луна, открой браузер")]))
    wake(h)
    wait_for(lambda: bool(h.replies))
    assert h.orch.requests[0].text == "открой браузер"


def test_no_speech_ends_quietly(make):
    h = make(script=[NO_SPEECH])
    wake(h)
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    assert h.stt.calls == []
    statuses = [e for e in h.events() if isinstance(e, StatusChanged)]
    assert [e.status for e in statuses] == [Status.LISTENING, Status.IDLE]
    assert statuses[-1].detail is None
    h.assert_holds_balanced()


# ---- busy / not live ---------------------------------------------------------------------


def test_on_wake_refused_when_busy(make):
    h = make(script=["block"])
    assert wake(h) is True
    wait_for(lambda: h.recorder.entered.is_set())
    assert wake(h) is False
    h.fe.stop()
    h.assert_holds_balanced()


def test_on_wake_refused_without_live_or_after_close(make):
    h = make(live=False)
    assert wake(h) is False
    h.fe.set_live(h.live, WakeWordSettings(), None)
    h.fe.close()
    assert wake(h) is False


def test_on_wake_refused_after_hotkey_press(make):
    h = make(script=["block"])
    h.hotkey.press()
    wait_for(lambda: h.recorder.entered.is_set())
    assert wake(h) is False
    h.fe.stop()


# ---- other turn kinds are not verified ---------------------------------------------------


def test_follow_up_turn_is_not_verified(make, caplog):
    caplog.set_level(logging.INFO, logger="luna.voice.frontend")
    h = make()
    assert wake(h, follow_up=True) is True
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.verifier.calls == []
    assert h.statuses()[0] is Status.LISTENING
    assert h.beeps == 0  # the cue is for the keyword only
    line = next(r.getMessage() for r in caplog.records if "voice turn" in r.getMessage())
    assert "trigger=follow_up" in line and "verify=-" in line
    h.assert_holds_balanced()


def test_hotkey_turn_in_live_mode_uses_a_tap_and_is_not_verified(make):
    h = make()
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.verifier.calls == []
    assert h.live.taps == [(VoiceSettings().preroll_ms, 0)]
    assert h.recorder.calls[0]["live"] is not None
    assert h.recorder.calls[0]["no_speech_s"] is None
    h.assert_holds_balanced()


def test_hotkey_barge_in_skips_the_echo_tail(make):
    h = make(wake=WakeWordSettings(speaking_tail_ms=400))
    h.speaker.active = True
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.live.taps == [(0, 400)]
    assert h.log[0] == "stop_speech"


def test_hotkey_turn_with_a_dead_listener_reports_a_microphone_error(make):
    h = make()
    h.live.tap_error = AudioError("wake listener is not running")
    h.hotkey.press()
    h.idle()
    assert h.statuses()[-1] is Status.ERROR
    assert h.stt.calls == []
    h.assert_holds_balanced()


# ---- holds on every exit path ------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["stt_error", "orchestrator_error", "recorder_error", "empty", "cancel"],
)
def test_holds_released_on_every_exit(make, case):
    script: list = []
    kw: dict = {}
    if case == "stt_error":
        kw["stt"] = FakeSTT([ProviderUnavailable("down")])
    elif case == "orchestrator_error":
        kw["orchestrator"] = FakeOrchestrator(RuntimeError("x"))
    elif case == "recorder_error":
        script = [AudioError("mic lost")]
    elif case == "empty":
        kw["stt"] = FakeSTT([Transcript("")])
    elif case == "cancel":
        script = ["block"]
    h = make(script, **kw)
    wake(h)
    if case == "cancel":
        wait_for(lambda: h.recorder.entered.is_set())
        h.fe.stop()
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    h.assert_holds_balanced()
    assert h.live.hold_log  # a hold was taken


def test_set_live_none_cancels_a_listening_turn(make):
    h = make(script=["block"])
    wake(h)
    wait_for(lambda: h.recorder.entered.is_set())
    h.fe.set_live(None)
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    assert h.stt.calls == []
    h.assert_holds_balanced()
    assert wake(h) is False


# ---- push-to-talk is unchanged -----------------------------------------------------------


def test_push_to_talk_without_live_uses_the_microphone_path(make):
    h = make([SPOKEN], live=False)
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert h.recorder.calls[0]["live"] is None
    assert h.recorder.calls[0]["no_speech_s"] is None
    assert h.live.hold_log == [] and h.live.taps == []
    assert h.statuses()[0] is Status.LISTENING


# ---- late verdicts, stale work, tap closed, start failure --------------------------------


class GatedVerifier(FakeVerifier):
    """``verify`` waits for ``gate`` (every call, or only the first with ``first_only``)."""

    def __init__(self, result=None, *, first_only: bool = False) -> None:
        super().__init__(result)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.first_only = first_only
        self._n = 0

    def verify(self, pcm16: bytes):
        self._n += 1
        self.entered.set()
        if not self.first_only or self._n == 1:
            self.gate.wait(5)
        return super().verify(pcm16)


def test_slow_reject_after_the_recording_ended_sends_nothing(make):
    verifier = GatedVerifier(VerifyResult(False, "no_keyword", "лунный"))
    h = make(script=[SPOKEN], verifier=verifier)
    assert wake(h) is True
    wait_for(lambda: verifier.entered.is_set())
    wait_for(lambda: h.recorder.active == 0 and bool(h.recorder.calls))
    time.sleep(0.1)  # the recording is over; the turn waits for the verdict
    assert h.stt.calls == [] and h.statuses() == []
    verifier.gate.set()  # the reject arrives late
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    assert h.stt.calls == [] and h.orch.requests == [] and h.replies == []
    assert h.statuses() == [] and h.beeps == 0 and "stop_speech" not in h.log
    h.assert_holds_balanced()


def test_late_verdict_does_not_touch_a_newer_turn(make):
    verifier = GatedVerifier(VerifyResult(True, "keyword", "луна"))
    h = make(script=["block", "block"], verifier=verifier)
    wake(h)
    wait_for(lambda: verifier.entered.is_set())
    h.hotkey.press()  # overrides the pending wake turn
    wait_for(lambda: len(h.recorder.calls) == 2)
    stops, beeps = h.log.count("stop_speech"), h.beeps
    verifier.gate.set()  # the stale accept arrives now
    time.sleep(0.2)
    assert h.log.count("stop_speech") == stops and h.beeps == beeps
    assert h.statuses() == [Status.LISTENING]  # only the hotkey turn announced itself
    assert not h.recorder.calls[1]["cancel"].is_set()
    assert h.fe.state is VoiceState.LISTENING
    h.fe.stop()


def test_overridden_turn_drops_its_queued_verification(make):
    verifier = GatedVerifier(first_only=True)
    h = make(script=["block", "block", "block", "frames"], verifier=verifier)
    wake(h)  # wake 0: its decode occupies the single worker
    wait_for(lambda: verifier.entered.is_set())
    h.hotkey.press()
    wait_for(lambda: len(h.recorder.calls) == 2)
    h.hotkey.press()  # early stop of the hotkey turn
    wait_for(lambda: bool(h.replies))
    h.idle()
    assert wake(h) is True  # wake A queues behind the stuck decode
    wait_for(lambda: len(h.recorder.calls) == 3)
    wait_for(lambda: h.fe._turn is not None and h.fe._turn.verify.future is not None)
    queued = h.fe._turn.verify.future
    h.hotkey.press()  # overrides A
    assert queued.cancelled()
    wait_for(lambda: len(h.replies) == 2)
    verifier.gate.set()
    h.idle()
    wait_for(lambda: len(verifier.calls) == 1)
    time.sleep(0.1)
    assert len(verifier.calls) == 1  # only wake 0 ever ran; A's work never reached the model


def test_closed_tap_ends_the_turn_without_waiting_for_the_verdict(make):
    verifier = GatedVerifier()
    h = make(script=["block"], verifier=verifier)
    trigger = h.live.trigger()
    assert h.fe.on_wake(trigger) is True
    wait_for(lambda: verifier.entered.is_set())
    started = time.monotonic()
    trigger.source.frames.close()  # the listener closed the tap
    h.idle()
    assert time.monotonic() - started < 1.5
    assert h.stt.calls == [] and h.statuses() == []
    h.assert_holds_balanced()
    verifier.gate.set()


def test_thread_start_failure_leaves_the_front_end_usable(make, monkeypatch):
    h = make(verifier=None)

    def boom(self):
        raise RuntimeError("no threads")

    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", boom)
        with pytest.raises(RuntimeError):
            wake(h)
    assert h.fe.state is VoiceState.IDLE and h.fe._turn is None
    h.assert_holds_balanced()
    assert wake(h) is True
    wait_for(lambda: bool(h.replies))


# ---- _TeeTap -----------------------------------------------------------------------------


def _frames(n: int) -> list[bytes]:
    return [bytes([i + 1]) * 960 for i in range(n)]


def test_tee_fires_once_with_the_first_frames_in_order():
    fired: list[list[bytes]] = []
    frames = _frames(5)
    tee = _TeeTap(FrameTap(frames), 3, fired.append)
    assert [tee.read(0) for _ in range(5)] == frames  # nothing lost or reordered
    assert fired == [frames[:3]]
    tee.close()
    assert len(fired) == 1


def test_tee_fires_with_a_partial_list_on_close():
    fired: list[list[bytes]] = []
    frames = _frames(2)
    tee = _TeeTap(FrameTap(frames), 24, fired.append)
    tee.read(0)
    tee.read(0)
    assert fired == []
    tee.close()
    tee.close()
    assert fired == [frames]
    assert tee.closed and tee.read(0) is None


def test_tee_fires_empty_when_closed_unread():
    fired: list[list[bytes]] = []
    _TeeTap(inner := FrameTap(_frames(2)), 3, fired.append).close()
    assert fired == [[]] and inner.closed


def test_tee_propagates_the_close_error_once():
    inner = FrameTap()
    tee = _TeeTap(inner, 3, lambda frames: None)
    inner.close(AudioError("mic lost"))
    assert tee.closed
    with pytest.raises(AudioError):
        tee.read(0)
    assert tee.read(0) is None
