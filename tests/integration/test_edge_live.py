# ruff: noqa: RUF001
"""Real Edge TTS (online) + playback. Manual:
``uv run pytest -m audio tests/integration/test_edge_live.py -s``."""

import sys
import time

import pytest

from luna.audio.playback import SoundDevicePlayer
from luna.tts.edge import EdgeTTS, FallbackTTS
from luna.tts.sapi import SapiTTS

pytestmark = [
    pytest.mark.audio,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows audio/SAPI"),
]


def _play(clip):
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


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Привет, я Луна. Чем могу помочь?", "ru"),
        ("Hello, I am Luna. How can I help?", "en"),
        ("Это довольно длинное предложение, " * 6, "ru"),
    ],
)
def test_real_edge_voice_speaks(text, lang):
    started = time.monotonic()
    clip = EdgeTTS().synthesize(text, lang)
    elapsed = time.monotonic() - started
    print(f"\n{lang} {len(text)} chars: {clip.duration_s:.2f}s audio in {elapsed:.2f}s")
    assert clip.sample_rate > 0 and 0.5 < clip.duration_s < 30
    _play(clip)


def test_network_down_falls_back_to_sapi(monkeypatch):
    """Simulate a dead network: the online fetch fails instantly -> SAPI speaks."""

    def no_network(*_a, **_k):
        raise OSError("network unreachable (simulated)")

    tts = FallbackTTS(EdgeTTS(fetcher=no_network), SapiTTS())
    started = time.monotonic()
    clip = tts.synthesize("Привет, интернета нет, говорю голосом Ирины.", "ru")
    print(f"\nfallback clip {clip.duration_s:.2f}s in {time.monotonic() - started:.2f}s")
    assert clip.pcm
    _play(clip)


def test_unreachable_endpoint_times_out_then_sapi(monkeypatch):
    """A real connection attempt to a dead host (TEST-NET-1) hits the timeout, then falls back."""
    import edge_tts.constants as c

    monkeypatch.setattr(c, "WSS_URL", "wss://192.0.2.1/tts", raising=False)
    monkeypatch.setattr("edge_tts.communicate.WSS_URL", "wss://192.0.2.1/tts", raising=False)
    tts = FallbackTTS(EdgeTTS(timeout_s=3), SapiTTS())
    started = time.monotonic()
    clip = tts.synthesize("Hello, the service is unreachable.", "en")
    assert time.monotonic() - started < 12
    assert clip.pcm


# --- #145: no stall longer than 5 s on the replies from the owner's log --------------------------

LOG_REPLIES = [
    ("Сейчас биткоин стоит примерно 79 764 доллара США (данные CoinMarketCap).", "ru"),
    ("Источник: coinmarketcap.com.", "ru"),
    ("Нарцисс — человек, который влюблён в себя.", "ru"),
    ("Это стоит 500 ₽.", "ru"),
    ("Сегодня в Москве облачно, около пяти градусов тепла. Вечером возможен дождь.", "ru"),
    ("Hello, I am Luna. How can I help?", "en"),
    ("Открываю браузер.", "ru"),
    ("Сейчас четырнадцать часов тридцать минут.", "ru"),
    ("Не удалось найти такое приложение. Попробуйте назвать его иначе.", "ru"),
    ("Готово.", "ru"),
]


@pytest.mark.live
@pytest.mark.parametrize(("text", "lang"), LOG_REPLIES)
def test_log_replies_speak_without_a_long_stall(text, lang):
    """Needs the network. Edge may fail (then FallbackTTS would use SAPI), but never for >5 s
    per attempt: first audio is bounded and one retry is allowed (so <= ~2 x 5 s worst case)."""
    from luna.tts.sapi import SapiTTS

    tts = FallbackTTS(EdgeTTS(), SapiTTS())
    started = time.monotonic()
    clip = tts.synthesize(text, lang)
    elapsed = time.monotonic() - started
    print(f"\n{len(text)} chars -> {clip.duration_s:.2f}s audio in {elapsed:.2f}s")
    assert clip.pcm
    assert elapsed < 12  # two bounded Edge attempts at the very worst, plus SAPI


@pytest.mark.live
def test_korean_is_skipped_not_failed():
    from luna.core.errors import NothingToSpeak

    with pytest.raises(NothingToSpeak):
        FallbackTTS(EdgeTTS(), SapiTTS()).synthesize("무엇을 도와드릴까요?", "ru")
