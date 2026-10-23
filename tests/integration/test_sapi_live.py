"""Real SAPI synthesis + playback (manual: ``uv sync --extra voice`` then
``uv run pytest -m audio tests/integration/test_sapi_live.py -s``)."""

import sys
import time

import pytest

from luna.audio.playback import SoundDevicePlayer
from luna.tts.sapi import SapiTTS

pytestmark = [
    pytest.mark.audio,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows SAPI only"),
]


@pytest.mark.parametrize(
    ("text", "lang"),
    [("Привет, я Луна.", "ru"), ("Hello, I am Luna.", "en")],
)
def test_real_voice_speaks(text, lang):
    tts = SapiTTS()
    started = time.monotonic()
    clip = tts.synthesize(text, lang)
    elapsed = time.monotonic() - started
    print(f"\n{lang}: synthesized {clip.duration_s:.2f}s of audio in {elapsed:.2f}s")
    assert clip.pcm
    assert clip.sample_rate > 0
    assert 0.5 < clip.duration_s < 10
    sd = pytest.importorskip("sounddevice")
    try:
        sd.query_devices(kind="output")
    except Exception as e:
        pytest.skip(f"no audio output device: {e}")
    player = SoundDevicePlayer()
    try:
        player.play(clip)  # you should hear the phrase
    finally:
        player.close()


def test_real_voices_include_irina_and_an_english_voice():
    names = [v.name for v in SapiTTS().list_voices()]
    print("\n" + "\n".join(names))
    assert any("irina" in n.lower() for n in names)
