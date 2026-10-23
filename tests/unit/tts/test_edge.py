import asyncio
import logging
import re

import pytest

from fakes import FakeTTS
from luna.audio.base import AudioClip
from luna.core.errors import ProviderRequestError, ProviderUnavailable
from luna.tts import edge
from luna.tts.base import TTSProvider
from luna.tts.edge import EdgeTTS, FallbackTTS, rate_to_percent, volume_to_percent

CLIP = AudioClip(pcm=b"\x01\x00" * 100, sample_rate=24000)


class Fetch:
    def __init__(self, data=b"mp3", error=None):
        self.data, self.error, self.calls = data, error, []

    def __call__(self, text, voice, rate, volume, timeout_s):
        self.calls.append((text, voice, rate, volume, timeout_s))
        if self.error:
            raise self.error
        return self.data


def make(fetch=None, decoder=None, **kw):
    return EdgeTTS(fetcher=fetch or Fetch(), decoder=decoder or (lambda _b: CLIP), **kw)


def test_satisfies_protocol():
    assert isinstance(make(), TTSProvider)
    assert make().name == "edge"


def test_speaks_ru_and_en_with_the_configured_voices():
    fetch = Fetch()
    tts = make(fetch)
    assert tts.synthesize("Привет", "ru") is CLIP
    tts.synthesize("Hello", "EN ")
    assert [c[1] for c in fetch.calls] == ["ru-RU-SvetlanaNeural", "en-US-JennyNeural"]
    assert fetch.calls[0][0] == "Привет"


def test_rate_volume_mapping():
    assert [rate_to_percent(r) for r in (-10, 0, 2, 10, 99)] == [
        "-50%",
        "+0%",
        "+10%",
        "+50%",
        "+50%",
    ]
    assert [volume_to_percent(v) for v in (100, 50, 0, -5, 500)] == [
        "+0%",
        "-50%",
        "-100%",
        "-100%",
        "+0%",
    ]
    fetch = Fetch()
    make(fetch, rate=-4, volume=80).synthesize("x", "en")
    assert fetch.calls[0][2:4] == ("-20%", "-20%")


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_text_is_a_request_error(text):
    with pytest.raises(ProviderRequestError):
        make().synthesize(text, "ru")


def test_unsupported_language_and_too_long_text():
    with pytest.raises(ProviderRequestError):
        make().synthesize("hola", "es")
    with pytest.raises(ProviderRequestError):
        make().synthesize("a" * (edge.MAX_TEXT_CHARS + 1), "en")


@pytest.mark.parametrize(
    "voice", ["", "Irina", 'x"><voice name="evil', "ru-RU-Svetlana", "ru-RU-X Neural"]
)
def test_invalid_voice_names_are_rejected(voice):
    with pytest.raises(ValueError, match="Edge neural voice"):
        EdgeTTS(voice_ru=voice)


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), OSError("net down"), TimeoutError(), ConnectionError()]
)
def test_service_errors_become_provider_unavailable(error):
    with pytest.raises(ProviderUnavailable):
        make(Fetch(error=error)).synthesize("x", "ru")


def test_timeout_message():
    with pytest.raises(ProviderUnavailable, match="timed out"):
        make(Fetch(error=TimeoutError()), timeout_s=3).synthesize("x", "ru")


def test_empty_or_undecodable_audio_is_unavailable():
    with pytest.raises(ProviderUnavailable, match="no audio"):
        make(Fetch(data=b"")).synthesize("x", "ru")

    def bad(_b):
        raise ValueError("not an mp3")

    with pytest.raises(ProviderUnavailable, match="decode"):
        make(decoder=bad).synthesize("x", "ru")
    with pytest.raises(ProviderUnavailable, match="empty"):
        make(decoder=lambda _b: AudioClip(b"", 24000)).synthesize("x", "ru")


def test_lone_surrogates_do_not_crash():
    fetch = Fetch()
    make(fetch).synthesize("a\ud800b", "en")
    fetch.calls[0][0].encode("utf-8")


def test_default_fetch_times_out(monkeypatch):
    """The real fetcher bounds the whole exchange with asyncio.wait_for."""

    class Hung:
        def __init__(self, *a, **k): ...

        async def stream(self):
            await asyncio.sleep(30)
            yield {"type": "audio", "data": b""}

    monkeypatch.setattr("edge_tts.Communicate", Hung)
    with pytest.raises(ProviderUnavailable):
        EdgeTTS(timeout_s=0.1, decoder=lambda _b: CLIP).synthesize("x", "ru")


def test_default_fetch_collects_only_audio_chunks(monkeypatch):
    class Fake:
        def __init__(self, text, voice, **kw):
            self.args = (text, voice, kw)

        async def stream(self):
            yield {"type": "WordBoundary", "text": "x"}
            yield {"type": "audio", "data": b"ab"}
            yield {"type": "audio", "data": b"cd"}

    monkeypatch.setattr("edge_tts.Communicate", Fake)
    got = []
    EdgeTTS(decoder=lambda b: got.append(b) or CLIP).synthesize("x", "en")
    assert got == [b"abcd"]


def test_real_decoder_turns_mp3_into_mono_int16(monkeypatch):
    class Decoded:
        class samples:
            @staticmethod
            def tobytes():
                return b"\x00\x01" * 10

        sample_rate = 24000

    seen = {}

    def fake_decode(data, **kw):
        seen.update(kw)
        return Decoded

    monkeypatch.setattr("miniaudio.decode", fake_decode)
    clip = edge._decode_mp3(b"mp3")
    assert (clip.sample_rate, len(clip.pcm)) == (24000, 20)
    assert seen["nchannels"] == 1
    assert seen["output_format"].name == "SIGNED16"


# --- SSML injection: edge-tts must escape the text -------------------------------------------

ATTACKS = [
    "</prosody></voice></speak><speak><voice name='x'>pwned",
    '<voice name="en-US-GuyNeural">hi</voice>',
    '<prosody rate="+100%">fast</prosody> & <break time="9s"/>',
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_markup_in_text_is_escaped_in_the_ssml(attack):
    import edge_tts
    from edge_tts.communicate import mkssml

    comm = edge_tts.Communicate(attack, "ru-RU-SvetlanaNeural", rate="+0%", volume="+0%")
    ssml = mkssml(comm.tts_config, next(comm.texts))
    body = re.search(r"<prosody[^>]*>(.*)</prosody>", ssml, re.S).group(1)
    assert "<" not in body and ">" not in body
    assert ssml.count("<voice ") == 1 and ssml.count("<speak ") == 1
    assert ssml.count("<prosody ") == 1
    assert "SvetlanaNeural" in ssml and "GuyNeural" not in ssml.split(">", 3)[2]


# --- FallbackTTS -----------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_fallback_uses_primary_when_healthy():
    primary, fallback = FakeTTS(name="edge"), FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback)
    tts.synthesize("a", "ru")
    assert len(primary.calls) == 1 and not fallback.calls
    assert tts.name == "edge+sapi"
    assert isinstance(tts, TTSProvider)


def test_fallback_speaks_via_fallback_warns_once_and_cools_down(caplog):
    clock = Clock()
    primary = FakeTTS(error=ProviderUnavailable("net down"), name="edge")
    fallback = FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback, cooldown_s=60, clock=clock)
    with caplog.at_level(logging.INFO, logger="luna.tts.edge"):
        for _ in range(3):
            assert tts.synthesize("a", "ru")
        assert len(primary.calls) == 1  # skipped during the cooldown
        assert len(fallback.calls) == 3
        clock.t += 61
        tts.synthesize("a", "ru")  # retried once the cooldown is over, fails again
    assert len(primary.calls) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # one warning per outage, not per sentence
    assert "net down" in warnings[0].getMessage()


def test_fallback_recovers_when_primary_comes_back(caplog):
    clock = Clock()
    primary = FakeTTS(error=ProviderUnavailable("down"), name="edge")
    fallback = FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback, cooldown_s=10, clock=clock)
    tts.synthesize("a", "en")
    primary.error = None
    clock.t += 11
    with caplog.at_level(logging.INFO, logger="luna.tts.edge"):
        tts.synthesize("b", "en")
        tts.synthesize("c", "en")
    assert len(primary.calls) == 3 and len(fallback.calls) == 1
    assert any("back" in r.getMessage() for r in caplog.records)


def test_request_errors_propagate_without_cooldown():
    primary = FakeTTS(error=ProviderRequestError("bad input"), name="edge")
    fallback = FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback)
    with pytest.raises(ProviderRequestError):
        tts.synthesize("a", "xx")
    primary.error = None
    tts.synthesize("a", "ru")
    assert len(primary.calls) == 2 and not fallback.calls


def test_fallback_failure_propagates():
    tts = FallbackTTS(
        FakeTTS(error=ProviderUnavailable("a"), name="edge"),
        FakeTTS(error=ProviderUnavailable("b"), name="sapi"),
    )
    with pytest.raises(ProviderUnavailable, match="b"):
        tts.synthesize("x", "ru")
