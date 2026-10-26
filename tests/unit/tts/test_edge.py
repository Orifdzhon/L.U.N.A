import asyncio
import logging
import re
import time

import pytest

from fakes import FakeTTS
from luna.audio.base import AudioClip
from luna.core.errors import NothingToSpeak, ProviderRequestError, ProviderUnavailable
from luna.tts import edge
from luna.tts.base import TTSProvider
from luna.tts.edge import EdgeTTS, FallbackTTS, rate_to_percent, volume_to_percent

CLIP = AudioClip(pcm=b"\x01\x00" * 100, sample_rate=24000)


class Fetch:
    def __init__(self, data=b"mp3", error=None):
        self.data, self.error, self.calls = data, error, []

    def __call__(self, text, voice, rate, volume, first_audio_s, total_s):
        self.calls.append((text, voice, rate, volume, first_audio_s, total_s))
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


class Script(FakeTTS):
    """Primary whose calls fail or succeed according to a script of booleans (True = ok)."""

    def __init__(self, script, name="edge"):
        super().__init__(name=name)
        self.script = list(script)

    def synthesize(self, text, lang):
        ok = self.script.pop(0) if self.script else True
        self.error = None if ok else ProviderUnavailable("stalled")
        return super().synthesize(text, lang)


def test_one_failure_moves_only_that_chunk_to_the_fallback(caplog):
    primary, fallback = Script([False, True, True]), FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback, clock=Clock())
    with caplog.at_level(logging.INFO, logger="luna.tts.edge"):
        tts.synthesize("one", "ru")  # edge fails -> sapi
        tts.synthesize("two", "ru")  # edge is tried again at once
        tts.synthesize("three", "ru")
    assert [c.text for c in primary.calls] == ["one", "two", "three"]
    assert [c.text for c in fallback.calls] == ["one"]
    assert any("back" in r.getMessage() for r in caplog.records)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_two_failures_in_a_row_back_off_then_edge_is_retried():
    clock = Clock()
    primary, fallback = Script([False, False]), FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback, cooldown_s=15, clock=clock)
    for text in ("a", "b", "c", "d"):
        tts.synthesize(text, "ru")
    assert [c.text for c in primary.calls] == ["a", "b"]  # c, d skipped during the backoff
    assert [c.text for c in fallback.calls] == ["a", "b", "c", "d"]
    clock.t += 16
    tts.synthesize("e", "ru")
    assert [c.text for c in primary.calls] == ["a", "b", "e"]
    assert len(fallback.calls) == 4  # edge answered


def test_a_success_resets_the_failure_count():
    primary, fallback = Script([False, True, False, True]), FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback, cooldown_s=60, clock=Clock())
    for text in ("a", "b", "c", "d"):
        tts.synthesize(text, "ru")
    assert len(primary.calls) == 4  # never two failures in a row: no backoff
    assert [c.text for c in fallback.calls] == ["a", "c"]


def test_failed_probe_after_backoff_backs_off_again():
    clock = Clock()
    primary, fallback = Script([False, False, False]), FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback, cooldown_s=15, clock=clock)
    tts.synthesize("a", "ru")
    tts.synthesize("b", "ru")
    clock.t += 16
    tts.synthesize("c", "ru")  # probe fails -> backoff again
    tts.synthesize("d", "ru")
    assert [c.text for c in primary.calls] == ["a", "b", "c"]


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


# --- stall handling ---------------------------------------------------------------------------


class Flaky:
    """Fetcher: the first ``fail`` calls raise ``error``, later ones return MP3 bytes."""

    def __init__(self, fail, error):
        self.fail, self.error, self.calls = fail, error, []

    def __call__(self, *args):
        self.calls.append(args)
        if len(self.calls) <= self.fail:
            raise self.error
        return b"mp3"


def test_one_immediate_retry_recovers_a_stalled_exchange():
    fetch = Flaky(1, TimeoutError())
    assert make(fetch).synthesize("Привет", "ru") is CLIP
    assert len(fetch.calls) == 2


def test_gives_up_after_the_retry():
    fetch = Flaky(5, RuntimeError("NoAudioReceived"))
    with pytest.raises(ProviderUnavailable, match="RuntimeError"):
        make(fetch).synthesize("Привет", "ru")
    assert len(fetch.calls) == edge.ATTEMPTS == 2


def test_empty_mp3_is_retried_too():
    data = iter([b"", b"mp3"])
    assert make(lambda *a: next(data)).synthesize("x", "ru") is CLIP


def test_timeouts_are_passed_and_scale_with_the_text_length():
    fetch = Fetch()
    tts = make(fetch, timeout_s=15, first_audio_timeout_s=4)
    tts.synthesize("Да.", "ru")
    tts.synthesize("слово " * 100, "ru")
    (_, _, _, _, first_a, total_a), (_, _, _, _, first_b, total_b) = fetch.calls
    assert first_a == first_b == 4
    assert 4 < total_a < 5  # short text: barely above the first-audio limit
    assert total_b == 15  # long text: capped by timeout_s


def test_total_timeout_formula():
    assert edge.total_timeout("", 4, 15) == 4
    assert edge.total_timeout("x" * 100, 4, 15) == pytest.approx(9)
    assert edge.total_timeout("x" * 10_000, 4, 15) == 15
    assert edge.total_timeout("x" * 10_000, 4, 3) == 3


@pytest.mark.parametrize("text", ["...", "—", "무엇을 도와드릴까요?", "  ! ? ", "₽ — …"])
def test_unspeakable_text_is_skipped_without_a_network_call(text):
    fetch = Fetch()
    with pytest.raises(NothingToSpeak):
        make(fetch).synthesize(text, "ru")
    assert fetch.calls == []


def test_mixed_script_text_is_still_sent():
    fetch = Fetch()
    make(fetch).synthesize("Корейский: 안녕", "ru")
    assert len(fetch.calls) == 1


def test_nothing_to_speak_does_not_start_a_backoff():
    primary = FakeTTS(error=NothingToSpeak("..."), name="edge")
    fallback = FakeTTS(name="sapi")
    tts = FallbackTTS(primary, fallback)
    for _ in range(3):
        with pytest.raises(NothingToSpeak):
            tts.synthesize("...", "ru")
    primary.error = None
    tts.synthesize("a", "ru")
    assert len(primary.calls) == 4 and not fallback.calls


def test_default_fetch_abandons_a_stalled_chunk_at_the_first_audio_timeout(monkeypatch):
    class Silent:
        def __init__(self, *a, **k): ...

        async def stream(self):
            await asyncio.sleep(30)  # no audio at all
            yield {"type": "audio", "data": b"x"}

    monkeypatch.setattr("edge_tts.Communicate", Silent)
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable, match="timed out"):
        EdgeTTS(timeout_s=30, first_audio_timeout_s=0.2, decoder=lambda _b: CLIP).synthesize(
            "x", "ru"
        )
    assert time.monotonic() - started < 3  # 2 attempts x 0.2 s, not 30 s


def test_default_fetch_total_timeout_applies_after_the_first_audio(monkeypatch):
    class Trickle:
        def __init__(self, *a, **k): ...

        async def stream(self):
            yield {"type": "audio", "data": b"x"}
            await asyncio.sleep(30)  # stalls after the first bytes
            yield {"type": "audio", "data": b"y"}

    monkeypatch.setattr("edge_tts.Communicate", Trickle)
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable):
        EdgeTTS(timeout_s=0.3, first_audio_timeout_s=0.1, decoder=lambda _b: CLIP).synthesize(
            "x", "ru"
        )
    assert time.monotonic() - started < 3


def test_fetch_passes_int_timeouts_to_edge_tts(monkeypatch):
    # edge-tts raises TypeError for float timeouts; our length-scaled limits are floats.
    seen = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kw):
            seen.update(kw)
            if not isinstance(kw["connect_timeout"], int) or not isinstance(
                kw["receive_timeout"], int
            ):
                raise TypeError("timeouts must be int")

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}

    fake_module = type("FakeEdgeTTS", (), {"Communicate": FakeCommunicate})
    monkeypatch.setitem(__import__("sys").modules, "edge_tts", fake_module)
    assert edge._fetch_mp3("Привет.", "v", "+0%", "+0%", 5.0, 5.35) == b"mp3"
    assert seen["connect_timeout"] == 5
    assert seen["receive_timeout"] == 6


def test_warm_up_sends_one_tiny_request_and_discards_it():
    fetch = Fetch()
    decoded = []
    tts = make(fetch, decoder=lambda b: decoded.append(b) or CLIP)
    tts.warm_up()
    assert len(fetch.calls) == 1
    assert len(fetch.calls[0][0]) <= 5
    assert decoded == []


@pytest.mark.parametrize("error", [ProviderUnavailable("down"), TimeoutError(), OSError("x")])
def test_warm_up_swallows_errors(error):
    make(Fetch(error=error)).warm_up()  # must not raise


def test_fallback_warm_up_warms_only_the_primary():
    fetch = Fetch()
    FallbackTTS(make(fetch), FakeTTS()).warm_up()
    assert len(fetch.calls) == 1


def test_fallback_warm_up_without_primary_support_is_a_noop():
    FallbackTTS(FakeTTS(), FakeTTS()).warm_up()
