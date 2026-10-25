"""VoiceFrontEnd: toggle push-to-talk state machine with fakes (no mic, no network)."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import pytest

from fakes import FakeClock, FakeHotkeyListener, FakeSTT
from luna.audio.recorder import Utterance
from luna.config.settings import STTSettings, VoiceSettings
from luna.core.errors import AudioError, ProviderUnavailable, ToolError
from luna.core.events import CLOSED, EventBus, Status, StatusChanged, TranscriptReady
from luna.core.models import Reply, Request
from luna.stt.base import Transcript
from luna.voice.frontend import (
    DETAIL_AUDIO_FAILED,
    DETAIL_NO_SPEECH,
    DETAIL_NOT_CAUGHT,
    DETAIL_STT_FAILED,
    VoiceFrontEnd,
    VoiceState,
)

PCM = b"\x01\x00" * 1600
SPOKEN = Utterance(PCM, 16000, 0.1, "silence")
NO_SPEECH = Utterance(b"", 16000, 0.0, "no_speech")
SECRET = "my secret sentence"


def ok(text: str = SECRET, lang: str = "ru") -> Transcript:
    return Transcript(text, lang, provider="fake")


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.002)
    raise AssertionError("condition not reached in time")


class FakeRecorder:
    """Script items: Utterance (returned at once), Exception (raised), or "block" (waits for
    stop() -> reason "stopped" with audio, or cancel -> "cancelled")."""

    def __init__(self, script: list, log: list[str]) -> None:
        self.script = list(script)
        self.log = log
        self.recording = False
        self.stop_calls = 0
        self.cancels: list[threading.Event] = []
        self._stop = threading.Event()
        self.started = threading.Event()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()

    def record(self, cancel: threading.Event) -> Utterance:
        self._stop.clear()
        self.cancels.append(cancel)
        self.log.append("mic_start")
        self.recording = True
        self.started.set()
        try:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if item == "block":
                while not (self._stop.is_set() or cancel.is_set()):
                    time.sleep(0.002)
                if cancel.is_set():
                    return Utterance(b"", 16000, 0.0, "cancelled")
                return Utterance(PCM, 16000, 0.1, "stopped")
            return item
        finally:
            self.recording = False
            self.log.append("mic_stop")


class FakeSpeaker:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1
        self.log.append("stop_speech")


class FakeOrchestrator:
    """Returns a reply, or with ``block`` waits for the cancel event."""

    def __init__(self, block: bool = False) -> None:
        self.block = block
        self.requests: list[Request] = []
        self.cancels: list[threading.Event] = []
        self.entered = threading.Event()

    def handle(self, request: Request, cancel: threading.Event | None = None) -> Reply:
        self.requests.append(request)
        assert cancel is not None
        self.cancels.append(cancel)
        self.entered.set()
        if self.block:
            cancel.wait(5)
            return Reply("cancelled", "en")
        return Reply("answer", request.lang or "ru")


class BlockingSTT:
    name = "blocking"

    def __init__(self, result: Transcript) -> None:
        self.result = result
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, pcm16, sample_rate=16000, *, lang_hint=None, prompt=None, timeout_s):
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        return self.result


class Harness:
    def __init__(self, script: list, stt=None, orchestrator=None, *, speaker=True, **kw) -> None:
        self.log: list[str] = []
        self.hotkey = FakeHotkeyListener()
        self.recorder = FakeRecorder(script, self.log)
        self.stt = stt if stt is not None else FakeSTT([ok()])
        self.orch = orchestrator or FakeOrchestrator()
        self.speaker = FakeSpeaker(self.log) if speaker else None
        self.bus = EventBus()
        self.sub = self.bus.subscribe()
        self.replies: list[Reply] = []
        self.fe = VoiceFrontEnd(
            self.hotkey,
            self.recorder,  # type: ignore[arg-type]
            self.stt,
            self.orch,
            self.speaker,
            self.bus,
            self.replies.append,
            voice=VoiceSettings(),
            stt_conf=STTSettings(),
            clock=kw.pop("clock", FakeClock()),
            **kw,
        )
        self.fe.start()

    def events(self) -> list:
        out = []
        while True:
            try:
                item = self.sub.queue.get_nowait()
            except queue.Empty:
                return out
            if item is not CLOSED:
                out.append(item)

    def statuses(self) -> list[tuple[Status, str | None]]:
        return [(e.status, e.detail) for e in self.events() if isinstance(e, StatusChanged)]

    def idle(self) -> None:
        wait_for(lambda: self.fe.state is VoiceState.IDLE)

    def close(self) -> None:
        self.fe.close()


@pytest.fixture
def make():
    made: list[Harness] = []

    def factory(script, **kw) -> Harness:
        h = Harness(script, **kw)
        made.append(h)
        return h

    yield factory
    for h in made:
        h.close()
        assert all(not t.is_alive() for t in h.fe._threads), "turn thread leaked"
        h.hotkey.running and pytest.fail("hotkey still running")


def test_happy_path_events_request_and_reply(make):
    h = make([SPOKEN])
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    h.idle()
    events = h.events()
    kinds = [type(e).__name__ for e in events]
    assert kinds[:3] == ["StatusChanged", "StatusChanged", "TranscriptReady"]
    assert events[0].status is Status.LISTENING
    assert events[1].status is Status.THINKING
    ready = events[2]
    assert isinstance(ready, TranscriptReady) and ready.lang == "ru"
    assert events[-1].status is Status.IDLE
    (request,) = h.orch.requests
    assert request.text == SECRET and request.source == "voice" and request.lang == "ru"
    assert request.request_id == events[0].request_id == ready.request_id
    assert h.replies[0].text == "answer"
    call = h.stt.calls[0]
    assert call.pcm16 == PCM and call.prompt is None and call.timeout_s == 5.0


def test_press_while_speaking_stops_speech_before_mic(make):
    h = make([SPOKEN])
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    assert h.log[:2] == ["stop_speech", "mic_start"]


def test_barge_in_after_reply_stops_speech_and_listens_again(make):
    h = make([SPOKEN, "block"], stt=FakeSTT([ok(), ok("second")]))
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    h.idle()
    h.log.clear()
    h.hotkey.press()  # the UI is "speaking" the reply now
    wait_for(lambda: h.recorder.recording)
    assert h.log == ["stop_speech", "mic_start"]
    assert h.fe.state is VoiceState.LISTENING


def test_no_speaker_is_fine(make):
    h = make([SPOKEN], speaker=False)
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))


def test_second_press_stops_recording_and_keeps_audio(make):
    h = make(["block"])
    h.hotkey.press()
    wait_for(lambda: h.recorder.recording)
    assert h.fe.state is VoiceState.LISTENING
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    assert h.recorder.stop_calls == 1
    assert h.stt.calls[0].pcm16 == PCM


def test_double_press_before_recorder_starts_is_no_speech(make):
    started = threading.Event()
    gate = threading.Event()

    def beep() -> None:  # holds the turn thread before the recorder starts
        started.set()
        gate.wait(3)

    h = make(["block"], beep=beep)
    h.hotkey.press()
    started.wait(3)
    h.hotkey.press()
    gate.set()
    h.idle()
    assert (Status.IDLE, DETAIL_NO_SPEECH) in h.statuses()
    assert h.stt.calls == [] and not h.recorder.started.is_set()


def test_no_speech_skips_stt_and_orchestrator(make):
    h = make([NO_SPEECH])
    h.hotkey.press()
    h.idle()
    assert h.stt.calls == [] and h.orch.requests == []
    assert h.statuses() == [(Status.LISTENING, None), (Status.IDLE, DETAIL_NO_SPEECH)]


def test_empty_transcript_sends_nothing(make):
    h = make([SPOKEN], stt=FakeSTT([Transcript("  ")]))
    h.hotkey.press()
    h.idle()
    assert h.orch.requests == [] and h.replies == []
    assert h.statuses() == [
        (Status.LISTENING, None),
        (Status.THINKING, None),
        (Status.IDLE, DETAIL_NOT_CAUGHT),
    ]


def test_unknown_language_becomes_none(make):
    h = make([SPOKEN], stt=FakeSTT([Transcript("hello", "unknown", provider="fake")]))
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    assert h.orch.requests[0].lang is None


def test_stt_failure_reports_error_then_next_press_works(make):
    h = make([SPOKEN, SPOKEN], stt=FakeSTT([ProviderUnavailable("all down"), ok("again")]))
    h.hotkey.press()
    h.idle()
    assert h.statuses()[-1] == (Status.ERROR, DETAIL_STT_FAILED)
    assert h.orch.requests == []
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    assert h.orch.requests[0].text == "again"


def test_audio_error_reports_error_and_recovers(make):
    h = make([AudioError("no mic"), SPOKEN])
    h.hotkey.press()
    h.idle()
    assert h.statuses() == [(Status.LISTENING, None), (Status.ERROR, DETAIL_AUDIO_FAILED)]
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))


def test_orchestrator_exception_becomes_error(make):
    class Boom(FakeOrchestrator):
        def handle(self, request, cancel=None):
            raise RuntimeError("boom")

    h = make([SPOKEN], orchestrator=Boom())
    h.hotkey.press()
    h.idle()
    assert (Status.ERROR, "request failed") in h.statuses()
    assert h.replies == []


def test_press_during_handling_cancels_request_and_listens(make):
    h = make(["block"], orchestrator=FakeOrchestrator(block=True))
    h.recorder.script = [SPOKEN, "block"]
    h.hotkey.press()
    assert h.orch.entered.wait(3)
    assert h.fe.state is VoiceState.HANDLING
    first_cancel = h.orch.cancels[0]
    h.hotkey.press()
    assert first_cancel.is_set()
    wait_for(lambda: h.recorder.recording)
    assert h.fe.state is VoiceState.LISTENING
    assert h.replies == []  # the abandoned reply is not delivered
    # the late status of the abandoned turn must not hide LISTENING
    wait_for(lambda: h.bus.last_status is not None and h.bus.last_status.status is Status.LISTENING)
    assert h.recorder.cancels[1] is not first_cancel


def test_press_during_transcribing_cancels_turn_and_listens(make):
    stt = BlockingSTT(ok())
    h = make([SPOKEN, "block"], stt=stt)
    h.hotkey.press()
    assert stt.entered.wait(3)
    assert h.fe.state is VoiceState.TRANSCRIBING
    h.hotkey.press()
    wait_for(lambda: h.recorder.recording)
    stt.release.set()
    wait_for(lambda: not h.fe._threads[0].is_alive())
    assert h.orch.requests == [] and h.replies == []
    assert h.fe.state is VoiceState.LISTENING


def test_rapid_presses_never_wedge(make):
    script = [SPOKEN] * 6
    h = make(script, stt=FakeSTT([ok()] * 6))
    for _ in range(6):
        h.hotkey.press()
    wait_for(lambda: h.fe.state is VoiceState.IDLE and not h.recorder.recording)
    h.hotkey.press()  # still usable
    h.recorder.script.append(SPOKEN)
    wait_for(lambda: h.fe.state is VoiceState.IDLE)


def test_close_while_listening(make):
    h = make(["block"])
    h.hotkey.press()
    wait_for(lambda: h.recorder.recording)
    h.fe.close()
    assert not h.recorder.recording
    assert h.fe.state is VoiceState.IDLE
    assert h.statuses()[-1][0] is Status.IDLE
    assert h.stt.calls == []


def test_close_while_transcribing(make):
    stt = BlockingSTT(ok())
    h = make([SPOKEN], stt=stt)
    h.hotkey.press()
    assert stt.entered.wait(3)
    h.fe.close(timeout=0.1)
    assert h.fe.state is VoiceState.IDLE
    stt.release.set()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))
    assert h.orch.requests == []


def test_close_while_handling(make):
    h = make([SPOKEN], orchestrator=FakeOrchestrator(block=True))
    h.hotkey.press()
    assert h.orch.entered.wait(3)
    h.fe.close()
    assert all(not t.is_alive() for t in h.fe._threads)
    assert h.replies == []


def test_press_after_close_is_ignored(make):
    h = make([SPOKEN])
    h.fe.close()
    assert not h.hotkey.running
    assert h.fe.state is VoiceState.IDLE
    with pytest.raises(RuntimeError):
        h.fe.start()


def test_start_propagates_taken_hotkey():
    hotkey = FakeHotkeyListener(error=ToolError("taken"))
    fe = VoiceFrontEnd(
        hotkey,
        None,  # type: ignore[arg-type]
        FakeSTT([]),
        FakeOrchestrator(),
        None,
        EventBus(),
        lambda r: None,
    )
    with pytest.raises(ToolError):
        fe.start()


def test_beep_hook_runs_before_microphone(make):
    log_holder: list[str] = []
    h = make([SPOKEN], beep=lambda: log_holder.append(",".join(h.log)))
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    assert log_holder == ["stop_speech"]  # beep fired after speech stopped, before mic_start


def test_failing_beep_does_not_break_turn(make):
    def beep() -> None:
        raise RuntimeError("no sound card")

    h = make([SPOKEN], beep=beep)
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))


def test_latency_line_has_spans_and_no_transcript(make, caplog):
    clock = FakeClock()
    caplog.set_level(logging.DEBUG, logger="luna.voice.frontend")
    h = make([SPOKEN], clock=clock)
    h.hotkey.press()
    wait_for(lambda: bool(h.replies))
    h.idle()
    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info) == 1
    line = info[0].getMessage()
    for key in ("hotkey_to_listening_ms=", "capture_ms=", "stt_ms=", "handle_ms=", "provider=fake"):
        assert key in line
    assert SECRET not in line
    assert not any(SECRET in r.getMessage() for r in caplog.records if r.levelno > logging.DEBUG)
    assert any(SECRET in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_no_latency_line_when_nothing_was_recognised(make, caplog):
    caplog.set_level(logging.INFO, logger="luna.voice.frontend")
    h = make([NO_SPEECH])
    h.hotkey.press()
    h.idle()
    assert not [r for r in caplog.records if r.levelno == logging.INFO]
