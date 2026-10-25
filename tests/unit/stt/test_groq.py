from __future__ import annotations

import io
import logging
import wave

import httpx
import pytest
import respx

from fakes import FakeClock, FakeSTT
from luna.core.errors import ProviderRequestError, ProviderUnavailable, RateLimited
from luna.stt.base import STTProvider, Transcript
from luna.stt.chain import STTChain
from luna.stt.groq import MAX_AUDIO_BYTES, GroqWhisperSTT

BASE = "https://stt.example.test/v1"
URL = f"{BASE}/audio/transcriptions"
KEY = "gsk_SUPERSECRETKEY1234567890"
PCM = bytes(range(256)) * 125  # 32 000 bytes = 1 s at 16 kHz, non-trivial content

HTTP = httpx.Client(trust_env=False, verify=False)


def make() -> GroqWhisperSTT:
    return GroqWhisperSTT(KEY, "test-whisper", BASE, http_client=HTTP)


def verbose(text="привет", language="russian", segments=None) -> dict:
    if segments is None:
        segments = [{"id": 0, "text": text, "avg_logprob": -0.1, "no_speech_prob": 0.01}]
    return {"task": "transcribe", "language": language, "duration": 1.0, "text": text,
            "segments": segments}  # fmt: skip


def run(stt: GroqWhisperSTT, **kw) -> Transcript:
    return stt.transcribe(PCM, 16_000, timeout_s=5.0, **kw)


def test_is_an_stt_provider() -> None:
    assert isinstance(make(), STTProvider)


@respx.mock
def test_request_carries_valid_wav_and_params() -> None:
    route = respx.post(URL).respond(200, json=verbose())
    result = run(make(), prompt="YouTube, Telegram")
    assert result.text == "привет"
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = request.content
    assert b'name="model"\r\n\r\ntest-whisper' in body
    assert b'name="temperature"\r\n\r\n0' in body
    assert b'name="response_format"\r\n\r\nverbose_json' in body
    assert b'name="prompt"\r\n\r\nYouTube, Telegram' in body
    assert b'name="language"' not in body
    start = body.index(b"RIFF")
    end = body.index(b"\r\n--", start)
    with wave.open(io.BytesIO(body[start:end])) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16_000)
        assert wav.readframes(wav.getnframes()) == PCM


@respx.mock
def test_language_hint_and_no_prompt() -> None:
    route = respx.post(URL).respond(200, json=verbose("hello", "english"))
    run(make(), lang_hint="en")
    body = route.calls.last.request.content
    assert b'name="language"\r\n\r\nen' in body
    assert b'name="prompt"' not in body


@respx.mock
def test_unknown_hint_is_not_sent() -> None:
    route = respx.post(URL).respond(200, json=verbose())
    run(make(), lang_hint="unknown")
    assert b'name="language"' not in route.calls.last.request.content


@respx.mock
@pytest.mark.parametrize(
    ("language", "expected"),
    [("russian", "ru"), ("english", "en"), ("ru", "ru"), ("german", "unknown")],
)
def test_language_normalised(language: str, expected: str) -> None:
    respx.post(URL).respond(200, json=verbose("x", language))
    assert run(make()).lang == expected


@respx.mock
def test_confidence_from_avg_logprob() -> None:
    segs = [{"avg_logprob": 0.0, "no_speech_prob": 0.0}, {"avg_logprob": -1e9}]
    respx.post(URL).respond(200, json=verbose(segments=segs))
    assert run(make()).confidence == pytest.approx(0.5)


@respx.mock
def test_confidence_none_without_segments() -> None:
    respx.post(URL).respond(200, json=verbose(segments=[]))
    result = run(make())
    assert result.confidence is None
    assert result.text == "привет"


@respx.mock
def test_silence_is_dropped() -> None:
    segs = [{"avg_logprob": -0.9, "no_speech_prob": 0.9}, {"no_speech_prob": 0.8}]
    respx.post(URL).respond(200, json=verbose("Thanks for watching!", "english", segs))
    assert run(make()).is_empty


@respx.mock
def test_mixed_segments_are_kept() -> None:
    segs = [
        {"avg_logprob": -0.2, "no_speech_prob": 0.9},
        {"avg_logprob": -0.2, "no_speech_prob": 0.1},
    ]
    respx.post(URL).respond(200, json=verbose("открой ютуб", "russian", segs))
    assert run(make()).text == "открой ютуб"


@respx.mock
def test_empty_text_is_empty_transcript() -> None:
    respx.post(URL).respond(200, json=verbose("  ", "russian"))
    assert run(make()).is_empty


@respx.mock
def test_rate_limited_with_retry_after() -> None:
    respx.post(URL).respond(429, headers={"Retry-After": "7"}, json={"error": {"message": "x"}})
    with pytest.raises(RateLimited) as info:
        run(make())
    assert info.value.retry_after_s == 7


@respx.mock
def test_rate_limited_retry_after_is_capped() -> None:
    respx.post(URL).respond(429, headers={"Retry-After": "999999"})
    with pytest.raises(RateLimited) as info:
        run(make())
    assert info.value.retry_after_s == 300.0


@respx.mock
def test_timeout_maps_to_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderUnavailable):
        run(make())


@respx.mock
def test_connection_error_maps_to_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderUnavailable):
        run(make())


@respx.mock
@pytest.mark.parametrize("status", [500, 503])
def test_server_error_maps_to_unavailable(status: int) -> None:
    respx.post(URL).respond(status, json={"error": {"message": "boom"}})
    with pytest.raises(ProviderUnavailable):
        run(make())


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
def test_client_errors_map_to_request_error(status: int) -> None:
    respx.post(URL).respond(status, json={"error": {"message": f"echo {KEY}"}})
    with pytest.raises(ProviderRequestError) as info:
        run(make())
    assert KEY not in str(info.value)
    assert not isinstance(info.value, (RateLimited, ProviderUnavailable))


@respx.mock
def test_malformed_response_maps_to_unavailable() -> None:
    respx.post(URL).respond(200, json={"nope": 1})
    with pytest.raises(ProviderUnavailable):
        run(make())


@respx.mock
def test_key_never_in_errors_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    stt = make()
    assert KEY not in repr(stt)
    for status in (401, 429, 500):
        respx.post(URL).respond(status, json={"error": {"message": KEY}})
        with pytest.raises(Exception) as info:
            run(stt)
        assert KEY not in str(info.value)
    assert KEY not in caplog.text


@respx.mock
def test_empty_audio_makes_no_network_call() -> None:
    route = respx.post(URL).respond(200, json=verbose())
    with pytest.raises(ProviderRequestError):
        make().transcribe(b"", 16_000, timeout_s=5.0)
    assert not route.called


@respx.mock
def test_too_short_odd_or_huge_audio_makes_no_network_call() -> None:
    route = respx.post(URL).respond(200, json=verbose())
    stt = make()
    for pcm in (b"\x00\x00" * 100, b"\x00" * 3201, b"\x00" * (MAX_AUDIO_BYTES + 2)):
        with pytest.raises(ProviderRequestError):
            stt.transcribe(pcm, 16_000, timeout_s=5.0)
    assert not route.called


@respx.mock
def test_rate_limited_groq_falls_through_in_chain() -> None:
    respx.post(URL).respond(429, headers={"Retry-After": "7"})
    fallback = FakeSTT([Transcript("hello", "en", 0.8)], name="local")
    chain = STTChain([make(), fallback], FakeClock())
    result = chain.transcribe(PCM, timeout_s=5.0)
    assert result.text == "hello"
    assert result.provider == "local"
