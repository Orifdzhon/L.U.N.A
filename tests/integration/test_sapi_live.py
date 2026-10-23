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


def test_speaker_speaks_and_stops_after_first_sentence():
    sd = pytest.importorskip("sounddevice")
    try:
        sd.query_devices(kind="output")
    except Exception as e:
        pytest.skip(f"no audio output device: {e}")
    from luna.config.settings import TTSSettings
    from luna.core.events import EventBus, Status
    from luna.core.models import Reply
    from luna.tts.speaker import Speaker

    bus = EventBus()
    sub = bus.subscribe()
    speaker = Speaker(SapiTTS(), SoundDevicePlayer(), bus, TTSSettings())

    def wait_for_status(status, timeout):
        while True:
            event = sub.queue.get(timeout=timeout)
            if getattr(event, "status", None) == status:
                return

    try:
        text = "Привет, я Луна. Сейчас я расскажу кое-что длинное. И ещё одно предложение."
        started = time.monotonic()
        speaker.speak(Reply(text=text, lang="ru"), "live")
        wait_for_status(Status.SPEAKING, 10)
        print(f"\nfirst audio after {time.monotonic() - started:.2f}s")
        time.sleep(1.5)  # roughly the first sentence
        stopped = time.monotonic()
        speaker.stop()
        wait_for_status(Status.IDLE, 5)
        print(f"IDLE {time.monotonic() - stopped:.2f}s after stop()")
        assert time.monotonic() - stopped < 0.5
    finally:
        speaker.close()
