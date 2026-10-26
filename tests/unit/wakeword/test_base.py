import pytest

from fakes import WAKE_MARKER, FakeWakeWord
from luna.wakeword.base import WAKE_SAMPLE_RATE, WakeEvent, WakeWordDetector

FRAME = b"\x00\x00" * 480


def test_fake_satisfies_protocol():
    assert isinstance(FakeWakeWord(), WakeWordDetector)


def test_triggers_on_scripted_index_with_end_sample():
    det = FakeWakeWord(trigger_at=[2])
    events = [det.process(FRAME) for _ in range(4)]
    assert events[:2] == [None, None]
    assert events[2] == WakeEvent("luna", 0.9, 3 * 480)
    assert events[3] is None


def test_triggers_on_marker_frame():
    det = FakeWakeWord()
    assert det.process(FRAME) is None
    event = det.process(WAKE_MARKER + FRAME[len(WAKE_MARKER) :])
    assert event is not None and event.end_sample == 2 * 480


def test_marker_can_be_disabled():
    det = FakeWakeWord(marker=False)
    assert det.process(WAKE_MARKER + FRAME[len(WAKE_MARKER) :]) is None


def test_reset_restarts_index_and_counts():
    det = FakeWakeWord(trigger_at=[0])
    assert det.process(FRAME) is not None
    assert det.process(FRAME) is None
    det.reset()
    assert det.reset_count == 1
    assert det.process(FRAME) == WakeEvent("luna", 0.9, 480)


def test_wrong_frame_size_and_closed_and_error():
    det = FakeWakeWord()
    with pytest.raises(ValueError):
        det.process(b"\x00\x00")
    det.close()
    assert det.closed
    with pytest.raises(RuntimeError):
        det.process(FRAME)
    with pytest.raises(OSError):
        FakeWakeWord(error=OSError("boom")).process(FRAME)


def test_wake_event_validation_and_seconds():
    assert WakeEvent("luna", end_sample=WAKE_SAMPLE_RATE).end_s == 1.0
    assert WakeEvent("luna").end_s is None
    with pytest.raises(ValueError):
        WakeEvent("")
    with pytest.raises(ValueError):
        WakeEvent("luna", confidence=1.5)
    with pytest.raises(ValueError):
        WakeEvent("luna", end_sample=-1)
