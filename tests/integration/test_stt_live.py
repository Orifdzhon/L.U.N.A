"""Real Groq Whisper call on speech synthesized by SAPI (manual:
``uv run pytest -m live tests/integration/test_stt_live.py -s``).

The key comes from the environment / ``.env`` (loaded only when a live test actually runs).
"""

from __future__ import annotations

import os
import sys

import pytest

from luna.config.settings import STTGroqSettings
from luna.stt.groq import GroqWhisperSTT
from luna.tts.sapi import SapiTTS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(sys.platform != "win32", reason="SAPI synthesis is Windows only"),
]


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [("открой ютуб", "ru", "ютуб"), ("what time is it", "en", "time")],
)
def test_groq_transcribes_sapi_speech(text: str, lang: str, expected: str) -> None:
    from dotenv import load_dotenv

    load_dotenv()
    cfg = STTGroqSettings()
    key = os.environ.get(cfg.api_key_env, "").strip()
    if not key:
        pytest.skip(f"{cfg.api_key_env} is not set")
    clip = SapiTTS().synthesize(text, lang)
    stt = GroqWhisperSTT(key, cfg.model)
    try:
        result = stt.transcribe(clip.pcm, clip.sample_rate, timeout_s=15.0)
    finally:
        stt.close()
    print(f"\n{lang}: {result}")
    assert expected in result.text.lower()
    assert result.lang == lang
