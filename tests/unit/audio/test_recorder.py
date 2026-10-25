import threading
import time
from collections.abc import Callable

import pytest
from test_vad import noise, silence, tone

from fakes import FakeClock, FakeMic
from luna.audio.recorder import Recorder
from luna.audio.vad import EnergyVAD
from luna.config.settings import VoiceSettings
from luna.core.errors import AudioError

FRAME_S = 0.03


class HookMic(FakeMic):
    """FakeMic that calls ``hook(read_index)`` before each read and can advance a clock."""

    def __init__(self, frames, hook: Callable[[int], None] | None = None, **kw) -> None:
        super().__init__(frames, **kw)
        self.hook = hook
        self.reads = 0

    def read(self, timeout_s: float) -> bytes | None:
        self.reads += 1
        if self.hook:
            self.hook(self.reads)
        return super().read(timeout_s)


def make(mic, **kw) -> Recorder:
    conf = VoiceSettings()
    return Recorder(mic, EnergyVAD(conf.vad_energy_threshold, conf.vad_noise_margin), conf, **kw)


def run(mic, cancel: threading.Event | None = None, **kw):
    return make(mic, **kw).record(cancel or threading.Event())


def test_silence_ending_with_preroll():
    mic = FakeMic(silence(10) + tone(27) + silence(30))
    u = run(mic)
    assert u.reason == "silence"
    assert u.sample_rate == 16000
    # 300 ms pre-roll + 800 ms tone + hangover + 700 ms silence
    assert 0.3 + 0.8 + 0.7 <= u.duration_s <= 0.3 + 0.8 + 0.7 + 0.15
    assert len(u.pcm16) == int(u.duration_s * 16000) * 2
    assert u.pcm16[: 10 * 960] == b"\x00" * (10 * 960)  # pre-roll is the leading silence
    assert mic.start_calls == 1 and mic.stop_calls == 1 and not mic.started


def test_endless_tone_hits_max_length():
    mic = FakeMic(silence(10) + tone(1) * 400)
    u = run(mic)
    assert u.reason == "max"
    assert 9.5 < u.duration_s <= 10 + FRAME_S
    assert mic.stop_calls == 1


def test_silence_only_gives_no_speech_after_wait():
    mic = FakeMic(silence(300))
    u = run(mic)
    assert u.reason == "no_speech"
    assert u.pcm16 == b""
    assert mic.stop_calls == 1


def test_click_shorter_than_min_speech_is_discarded():
    mic = FakeMic(silence(10) + tone(3) + silence(200))
    u = run(mic)
    assert u.reason == "no_speech"
    assert u.pcm16 == b""


def test_speech_after_a_click_is_still_recorded():
    mic = FakeMic(silence(10) + tone(3) + silence(10) + tone(20) + silence(40))
    u = run(mic)
    assert u.reason == "silence"
    assert u.duration_s > 0.6


def test_steady_hum_is_not_speech():
    mic = FakeMic(noise(300))
    assert run(mic).reason == "no_speech"


def test_cancel_mid_recording_returns_within_one_frame():
    cancel = threading.Event()
    mic = HookMic(silence(10) + tone(100), hook=lambda n: cancel.set() if n == 20 else None)
    u = run(mic, cancel)
    assert u.reason == "cancelled"
    assert u.pcm16 == b""
    assert mic.reads == 20  # no frame read after cancel was set
    assert mic.stop_calls == 1


def test_cancel_set_before_start():
    cancel = threading.Event()
    cancel.set()
    mic = FakeMic(tone(10))
    u = run(mic, cancel)
    assert u.reason == "cancelled"
    assert mic.stop_calls == 1


def test_stop_keeps_the_audio_heard_so_far():
    holder: dict[str, Recorder] = {}
    mic = HookMic(
        silence(10) + tone(100),
        hook=lambda n: holder["r"].stop() if n == 30 else None,
    )
    holder["r"] = make(mic)
    u = holder["r"].record(threading.Event())
    assert u.reason == "stopped"
    assert u.duration_s > 0.5
    assert mic.stop_calls == 1


def test_stop_before_speech_gives_empty_result():
    holder: dict[str, Recorder] = {}
    mic = HookMic(silence(50), hook=lambda n: holder["r"].stop() if n == 5 else None)
    holder["r"] = make(mic)
    u = holder["r"].record(threading.Event())
    assert u.reason == "stopped" and u.pcm16 == b""


def test_recorder_can_be_reused_after_stop():
    rec = make(FakeMic(silence(10) + tone(20) + silence(40)))
    rec.stop()
    u = rec.record(threading.Event())  # a stale stop() must not end the next recording
    assert u.reason == "silence"


def test_per_call_stop_set_before_record_is_not_lost():
    stop = threading.Event()
    stop.set()  # requested just before record() starts
    mic = FakeMic(silence(10) + tone(20) + silence(40))
    u = make(mic).record(threading.Event(), stop=stop)
    assert u.reason == "stopped" and u.pcm16 == b""
    assert mic.stop_calls == 1


def test_per_call_stop_keeps_audio_heard_so_far():
    stop = threading.Event()
    mic = HookMic(silence(10) + tone(100), hook=lambda n: stop.set() if n == 30 else None)
    u = make(mic).record(threading.Event(), stop=stop)
    assert u.reason == "stopped" and u.duration_s > 0.5


def test_audio_error_during_read_propagates_and_stops_mic():
    mic = FakeMic(silence(10) + tone(3), read_error=AudioError("unplugged"))
    with pytest.raises(AudioError, match="unplugged"):
        run(mic)
    assert mic.stop_calls == 1


def test_audio_error_on_start_still_stops_mic():
    mic = FakeMic(error=AudioError("busy"))
    with pytest.raises(AudioError, match="busy"):
        run(mic)
    assert mic.stop_calls == 1


def test_stalled_mic_raises_instead_of_hanging():
    clock = FakeClock()
    mic = HookMic([], hook=lambda n: clock.advance(0.5))
    with pytest.raises(AudioError, match="no audio"):
        run(mic, clock=clock)
    assert mic.stop_calls == 1


def test_cancel_from_another_thread():
    class EndlessMic(FakeMic):
        def read(self, timeout_s):
            time.sleep(0.001)
            return silence()[0]

    mic = EndlessMic()
    cancel = threading.Event()
    out = []
    t = threading.Thread(target=lambda: out.append(make(mic).record(cancel)))
    t.start()
    time.sleep(0.05)
    cancel.set()
    t.join(timeout=3)
    assert not t.is_alive()
    assert out[0].reason == "cancelled"
    assert mic.stop_calls == 1


def test_no_preroll_when_disabled():
    conf = VoiceSettings(preroll_ms=0)
    mic = FakeMic(silence(10) + tone(20) + silence(40))
    u = Recorder(mic, EnergyVAD(conf.vad_energy_threshold, conf.vad_noise_margin), conf).record(
        threading.Event()
    )
    assert u.reason == "silence"
    assert u.pcm16[:960] != b"\x00" * 960
