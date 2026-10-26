import array
import math
import random

import pytest

from luna.audio.vad import EnergyVAD, rms_level

RATE = 16000
FRAME_MS = 30
SAMPLES = RATE * FRAME_MS // 1000


def silence(n: int = 1) -> list[bytes]:
    return [b"\x00\x00" * SAMPLES] * n


def tone(n: int = 1, amplitude: float = 0.2, freq: float = 440.0) -> list[bytes]:
    frames = []
    pos = 0
    for _ in range(n):
        data = array.array(
            "h",
            (
                int(amplitude * 32767 * math.sin(2 * math.pi * freq * (pos + i) / RATE))
                for i in range(SAMPLES)
            ),
        )
        pos += SAMPLES
        frames.append(data.tobytes())
    return frames


def noise(n: int = 1, amplitude: float = 0.03, seed: int = 1) -> list[bytes]:
    rng = random.Random(seed)
    peak = int(amplitude * 32767)
    return [
        array.array("h", (rng.randint(-peak, peak) for _ in range(SAMPLES))).tobytes()
        for _ in range(n)
    ]


def feed(vad: EnergyVAD, frames: list[bytes]) -> list[bool]:
    return [vad.is_speech(f) for f in frames]


def test_rms_level_known_values():
    assert rms_level(b"") == 0.0
    assert rms_level(silence()[0]) == 0.0
    assert rms_level(tone(1, 0.5)[0]) == pytest.approx(0.5 / math.sqrt(2), rel=0.02)
    assert rms_level(b"\x01") == 0.0  # odd trailing byte is ignored


def test_silence_is_never_speech():
    vad = EnergyVAD()
    assert not any(feed(vad, silence(40)))


def test_warmup_reports_no_speech_then_tone_is_detected():
    vad = EnergyVAD()
    out = feed(vad, silence(8) + tone(5))
    assert out[:8] == [False] * 8
    assert out[8:] == [True] * 5


def test_hum_is_background_after_warmup():
    vad = EnergyVAD()
    assert not any(feed(vad, noise(60)))
    assert vad.noise_floor > 0.005


def test_speech_over_hum_is_detected():
    vad = EnergyVAD()
    feed(vad, noise(10))
    assert all(feed(vad, tone(5, 0.3)))


def test_quiet_sound_below_threshold_is_not_speech():
    vad = EnergyVAD(threshold=0.05)
    assert not any(feed(vad, silence(8) + tone(10, 0.02)))


def test_hangover_bridges_a_short_dip_only():
    vad = EnergyVAD(hangover_ms=60)
    feed(vad, silence(8) + tone(3))
    assert feed(vad, silence(4)) == [True, True, False, False]


def test_reset_restarts_warmup():
    vad = EnergyVAD()
    feed(vad, silence(8))
    assert vad.is_speech(tone()[0])
    vad.reset()
    assert vad.is_speech(tone()[0]) is False


def test_invalid_parameters_rejected():
    with pytest.raises(ValueError):
        EnergyVAD(threshold=0)
    with pytest.raises(ValueError):
        EnergyVAD(margin=0.5)


def test_reset_with_noise_floor_skips_warmup():
    vad = EnergyVAD()
    vad.reset(noise_floor=0.001)
    assert vad.noise_floor == 0.001
    assert vad.is_speech(tone()[0])


def test_reset_with_noise_floor_raises_the_threshold():
    vad = EnergyVAD()
    vad.reset(noise_floor=0.1)
    assert vad.is_speech(tone()[0]) is False  # rms ~0.14 < 0.1 * 3


def test_reset_rejects_negative_noise_floor():
    with pytest.raises(ValueError):
        EnergyVAD().reset(noise_floor=-0.1)
