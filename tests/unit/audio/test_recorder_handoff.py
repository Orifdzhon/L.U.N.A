"""Recorder with a live source (wake mode, #107): tap hand-off, lead-in rules, no mic access."""

import threading

import pytest
from test_vad import silence, tone

from fakes import FakeFrameSource, FakeMic
from luna.audio.capture import FrameTap, LiveSource
from luna.audio.recorder import Recorder
from luna.audio.vad import EnergyVAD
from luna.config.settings import VoiceSettings
from luna.core.errors import AudioError

FRAME = 960  # bytes in a 30 ms frame
LEAD = b"\x01\x00" * 4800  # 300 ms, easy to recognise in the result


class SpyVAD(EnergyVAD):
    def __init__(self, conf: VoiceSettings) -> None:
        self.fed: list[bytes] = []
        self.resets: list[float | None] = []
        super().__init__(conf.vad_energy_threshold, conf.vad_noise_margin)
        self.resets.clear()  # the constructor's own reset() is not part of the recording

    def is_speech(self, frame: bytes) -> bool:
        self.fed.append(frame)
        return super().is_speech(frame)

    def reset(self, noise_floor: float | None = None) -> None:
        self.resets.append(noise_floor)
        super().reset(noise_floor)


def record(frames, *, lead_in=b"", floor=0.0, mic=None, cancel=None, vad=None, **kw):
    conf = VoiceSettings()
    mic = mic or FakeMic()
    vad = vad or SpyVAD(conf)
    tap = frames if hasattr(frames, "close") else FrameTap(frames)
    live = LiveSource(tap, lead_in, floor)
    u = Recorder(mic, vad, conf).record(cancel or threading.Event(), live=live, **kw)
    return u, tap, mic, vad


def test_live_never_touches_the_mic_and_closes_the_tap():
    u, tap, mic, vad = record(tone(27) + silence(30))
    assert u.reason == "silence"
    assert mic.start_calls == 0 and mic.stop_calls == 0 and not mic.closed
    assert tap.closed
    assert vad.resets == [0.0]


def test_live_vad_is_seeded_without_warmup():
    # speech from the very first frame is detected (push-to-talk would treat it as background)
    u, *_ = record(tone(27) + silence(30), lead_in=LEAD)
    assert u.reason == "silence"
    assert u.pcm16.startswith(LEAD + tone(1)[0])


def test_seeded_noise_floor_raises_the_speech_threshold():
    u, *_ = record(tone(200), floor=0.1)  # tone rms ~0.14 < 0.1 * margin
    assert u.reason == "no_speech"


def test_missing_noise_floor_means_normal_warmup():
    _, _, _, vad = record(silence(5), floor=None, no_speech_s=0.1)
    assert vad.resets == [None]


def test_lead_in_is_prepended_when_speech_is_within_preroll():
    u, *_ = record(silence(10) + tone(27) + silence(30), lead_in=LEAD)  # gap == 10 frames
    assert u.pcm16.startswith(LEAD + silence(1)[0] * 10)
    assert len(u.pcm16) > len(LEAD) + 10 * FRAME


def test_lead_in_is_dropped_when_the_gap_exceeds_the_preroll():
    u, *_ = record(silence(11) + tone(27) + silence(30), lead_in=LEAD)
    assert LEAD not in u.pcm16
    assert u.pcm16.startswith(silence(1)[0] * 10)  # only the 300 ms pre-roll


def test_lead_in_counts_clicks_before_the_speech():
    u, *_ = record(silence(6) + tone(2) + silence(6) + tone(27) + silence(30), lead_in=LEAD)
    assert LEAD not in u.pcm16  # 14 frames passed before the real speech


def test_lead_in_never_reaches_the_vad():
    _, _, _, vad = record(tone(27) + silence(30), lead_in=LEAD)
    assert LEAD not in vad.fed and all(len(f) == FRAME for f in vad.fed)


def test_lead_in_is_not_returned_without_speech():
    u, *_ = record(silence(300), lead_in=LEAD)
    assert u.reason == "no_speech" and u.pcm16 == b""


def test_no_speech_s_override_ends_early():
    source = FakeFrameSource(silence(300))
    u, *_ = record(source, no_speech_s=1.0)
    assert u.reason == "no_speech"
    assert source.reads <= 36  # ~1 s of 30 ms frames, not the default 5 s
    assert source.close_calls == 1


def test_no_speech_s_longer_than_default_waits_longer():
    u, *_ = record(silence(250) + tone(27) + silence(30), no_speech_s=9.0)
    assert u.reason == "silence"


def test_no_speech_s_also_applies_to_push_to_talk():
    mic = FakeMic(silence(300))
    u = Recorder(mic, EnergyVAD(), VoiceSettings()).record(threading.Event(), no_speech_s=0.5)
    assert u.reason == "no_speech" and mic.stop_calls == 1


def test_tap_closed_without_error_is_cancelled():
    tap = FrameTap(tone(5))
    tap.close()
    u, *_ = record(tap)
    assert u.reason == "cancelled" and u.pcm16 == b""


def test_tap_closed_while_recording_is_cancelled():
    tap = FrameTap(tone(30))

    class ClosingVAD(SpyVAD):
        def is_speech(self, frame: bytes) -> bool:
            if len(self.fed) == 5:
                tap.close()
            return super().is_speech(frame)

    u, *_ = record(tap, vad=ClosingVAD(VoiceSettings()))
    assert u.reason == "cancelled" and u.pcm16 == b""


def test_tap_closed_with_error_raises():
    tap = FrameTap(tone(2))
    tap.close(AudioError("microphone lost"))
    with pytest.raises(AudioError, match="microphone lost"):
        record(tap)
    assert tap.closed


def test_tap_is_closed_on_cancel_stop_and_errors():
    cancel = threading.Event()
    cancel.set()
    u, tap, mic, _ = record(tone(5), cancel=cancel)
    assert u.reason == "cancelled" and tap.closed and mic.stop_calls == 0

    stop = threading.Event()
    stop.set()
    u, tap, *_ = record(tone(5), stop=stop)
    assert u.reason == "stopped" and tap.closed

    source = FakeFrameSource(read_error=AudioError("boom"))
    with pytest.raises(AudioError, match="boom"):
        record(source)
    assert source.close_calls == 1


def test_stop_after_speech_keeps_audio_with_lead_in():
    stop = threading.Event()

    class StoppingSource(FakeFrameSource):
        def read(self, timeout_s):
            if len(self._frames) == 8:
                stop.set()
            return super().read(timeout_s)

    u, *_ = record(StoppingSource(tone(20)), lead_in=LEAD, stop=stop)
    assert u.reason == "stopped" and u.pcm16.startswith(LEAD)
