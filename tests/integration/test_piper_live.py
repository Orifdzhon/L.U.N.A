"""Real Piper voices through sherpa-onnx. Manual: ``uv run pytest -m live -k piper -q -s``.

The first run downloads the two voices (about 130 MB) into ``%LOCALAPPDATA%\\LUNA\\models``.
"""

import statistics
import time

import pytest

from luna.tts.piper import PiperTTS

pytestmark = pytest.mark.live

SENTENCE = "Привет, я Луна. Чем могу помочь?"
LIMIT_S = 0.5


@pytest.fixture(scope="module")
def tts() -> PiperTTS:
    provider = PiperTTS()
    provider.warm_up()  # downloads and loads the Russian voice; the English one loads lazily
    return provider


def test_irina_first_audio_under_half_a_second_after_warm_up(tts):
    timings = []
    for _ in range(5):
        started = time.monotonic()
        clip = tts.synthesize(SENTENCE, "ru")
        timings.append(time.monotonic() - started)
        assert clip.sample_rate > 0 and 0.5 < clip.duration_s < 10
    print(
        f"\nirina: median {statistics.median(timings) * 1000:.0f} ms, "
        f"max {max(timings) * 1000:.0f} ms for {clip.duration_s:.2f} s of audio"
    )
    assert max(timings) < LIMIT_S


def test_numbers_and_brands_synthesize(tts):
    clip = tts.synthesize("Открываю YouTube. Это стоит 79 764 ₽, скидка 25%.", "ru")
    assert clip.duration_s > 2


def test_kristin_speaks_english(tts):
    started = time.monotonic()
    tts.synthesize("Warming up.", "en")  # loads the model
    started = time.monotonic()
    clip = tts.synthesize("Hello, I am Luna. How can I help?", "en")
    elapsed = time.monotonic() - started
    print(f"\nkristin: {elapsed * 1000:.0f} ms for {clip.duration_s:.2f} s of audio")
    assert elapsed < LIMIT_S
