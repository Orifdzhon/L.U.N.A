"""Real faster-whisper on CPU. Downloads ``small`` (about 465 MB) into %LOCALAPPDATA%\\LUNA\\models
on the first run (raise the 60 s timeout: ``--timeout=900``). Manual:
``uv run pytest tests/integration/test_stt_faster_whisper_live.py -m live -s --timeout=900``.

The audio is synthesized with SAPI (so it is clean speech: this checks plumbing and rough
accuracy, not real-microphone robustness).
"""

import sys
import time

import numpy as np
import pytest

from luna.stt.faster_whisper import FasterWhisperSTT
from luna.tts.sapi import SapiTTS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows SAPI only"),
]


def to_16k(clip):
    samples = np.frombuffer(clip.pcm, dtype="<i2").astype(np.float32)
    n = round(len(samples) * 16_000 / clip.sample_rate)
    resampled = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    return resampled.astype("<i2").tobytes()


@pytest.fixture(scope="module")
def stt():
    provider = FasterWhisperSTT()
    started = time.monotonic()
    provider.warm_up()
    print(f"\nmodel load (incl. any download): {time.monotonic() - started:.1f}s")
    return provider


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [("Открой ютуб", "ru", ("ютуб", "youtube")), ("What time is it", "en", ("time",))],
)
def test_transcribes_synthesized_speech(stt, text, lang, expected):
    pcm = to_16k(SapiTTS().synthesize(text, lang))
    seconds = len(pcm) / 2 / 16_000
    for attempt in ("first", "second"):
        started = time.monotonic()
        result = stt.transcribe(pcm, 16_000, timeout_s=30)
        elapsed = time.monotonic() - started
        print(
            f"{lang} {attempt}: {result.text!r} lang={result.lang} conf={result.confidence} "
            f"{elapsed:.2f}s for {seconds:.2f}s audio (RTF {elapsed / seconds:.2f})"
        )
    assert result.lang == lang
    assert any(word in result.text.lower() for word in expected)
