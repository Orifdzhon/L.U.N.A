"""``STTProvider`` over Groq's OpenAI-compatible ``audio.transcriptions`` endpoint (ADR 0003).

Audio is wrapped into an in-memory WAV (no temp files). Error mapping follows
``luna.llm.openai_compat``: the SDK's own retries are disabled (``max_retries=0``, fallback is
the job of ``STTChain``), ``trust_env=False`` keeps proxies away from the key, and the API key
lives only inside the SDK client: never in logs, exception messages or ``repr()``.
"""

from __future__ import annotations

import io
import logging
import math
import wave
from typing import Any

import httpx
import openai
from pydantic import SecretStr

from luna.core.errors import (
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
)
from luna.llm.openai_compat import _parse_retry_after
from luna.stt.base import UNKNOWN_LANG, Transcript, normalize_lang

log = logging.getLogger(__name__)
for _noisy in ("openai", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq free-tier upload limit
MIN_AUDIO_S = 0.1
# Whisper's own convention: a segment is "no speech" above ~0.6.
NO_SPEECH_THRESHOLD = 0.6
_SAMPLE_WIDTH = 2


def pcm16_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    """Mono 16-bit WAV container around raw PCM (in memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(_SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16)
    return buf.getvalue()


class GroqWhisperSTT:
    name = "groq"

    def __init__(
        self,
        api_key: SecretStr | str,
        model: str = "whisper-large-v3-turbo",
        base_url: str = DEFAULT_BASE_URL,
        *,
        connect_timeout_s: float = 5.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self._connect_timeout_s = connect_timeout_s
        if isinstance(api_key, SecretStr):
            api_key = api_key.get_secret_value()
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key.strip(),
            max_retries=0,
            http_client=http_client or httpx.Client(trust_env=False),
        )

    def __repr__(self) -> str:
        return f"GroqWhisperSTT(model={self.model!r})"

    def close(self) -> None:
        self._client.close()

    def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = 16_000,
        *,
        lang_hint: str | None = None,
        prompt: str | None = None,
        timeout_s: float,
    ) -> Transcript:
        if sample_rate <= 0:
            raise ProviderRequestError(f"{self.name}: invalid sample rate")
        if len(pcm16) % _SAMPLE_WIDTH:
            raise ProviderRequestError(f"{self.name}: PCM must hold whole int16 samples")
        if not pcm16:
            raise ProviderRequestError(f"{self.name}: empty audio")
        duration_s = len(pcm16) / _SAMPLE_WIDTH / sample_rate
        if duration_s < MIN_AUDIO_S:
            raise ProviderRequestError(f"{self.name}: audio is shorter than {MIN_AUDIO_S} s")
        wav_bytes = pcm16_to_wav(pcm16, sample_rate)
        if len(wav_bytes) > MAX_AUDIO_BYTES:
            raise ProviderRequestError(f"{self.name}: audio exceeds the 25 MB upload limit")

        params: dict[str, Any] = {
            "model": self.model,
            "file": ("audio.wav", wav_bytes, "audio/wav"),
            "response_format": "verbose_json",
            "temperature": 0,
        }
        hint = (lang_hint or "").strip().lower()
        if hint and hint != UNKNOWN_LANG:
            params["language"] = hint
        if prompt and prompt.strip():
            params["prompt"] = prompt.strip()
        timeout = httpx.Timeout(timeout_s, connect=min(self._connect_timeout_s, timeout_s))
        log.debug("%s: transcribe model=%s audio=%.2fs", self.name, self.model, duration_s)
        try:
            result = self._client.audio.transcriptions.create(timeout=timeout, **params)
            return self._parse(result)
        except ProviderError:
            raise
        except openai.APIStatusError as exc:
            raise self._map_status_error(exc) from None
        except openai.APITimeoutError:
            raise ProviderUnavailable(f"{self.name}: request timed out") from None
        except openai.APIConnectionError:
            raise ProviderUnavailable(f"{self.name}: cannot connect to the server") from None
        except openai.OpenAIError as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}") from None
        except (ValueError, AttributeError, TypeError, KeyError):
            raise ProviderUnavailable(f"{self.name}: malformed response") from None

    # -- response ------------------------------------------------------------------------------

    def _parse(self, result: Any) -> Transcript:
        if isinstance(result, str):  # plain-text fallback from a non-verbose server
            return Transcript(result.strip())
        text = _field(result, "text")
        if not isinstance(text, str):
            raise ProviderUnavailable(f"{self.name}: malformed response")
        text = text.strip()
        segments = [s for s in (_field(result, "segments") or []) if s is not None]
        no_speech = [_number(_field(s, "no_speech_prob")) for s in segments]
        if no_speech and all(p is not None and p > NO_SPEECH_THRESHOLD for p in no_speech):
            return Transcript("")  # Whisper hallucinates text on silence; drop it
        if not text:
            return Transcript("")
        lang_raw = _field(result, "language")
        lang = normalize_lang(lang_raw) if isinstance(lang_raw, str) else UNKNOWN_LANG
        return Transcript(text, lang, _confidence(segments))

    # -- errors --------------------------------------------------------------------------------

    def _map_status_error(self, exc: openai.APIStatusError) -> ProviderError:
        status = exc.status_code
        if status == 429:
            retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
            return RateLimited(f"{self.name}: rate limited (HTTP 429)", retry_after_s=retry_after)
        if status >= 500 or status in (408, 409):
            return ProviderUnavailable(f"{self.name}: server error (HTTP {status})")
        if status in (401, 403):
            return ProviderRequestError(
                f"{self.name}: authentication failed (HTTP {status}); check the API key and access"
            )
        # The body is not echoed: it may quote the request (prompt, file name).
        return ProviderRequestError(f"{self.name}: request rejected (HTTP {status})")


def _field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    value = getattr(obj, key, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            return extra.get(key)
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _confidence(segments: list[Any]) -> float | None:
    """Mean ``exp(avg_logprob)`` over segments, clamped to 0..1; ``None`` if not reported."""
    probs = []
    for segment in segments:
        logprob = _number(_field(segment, "avg_logprob"))
        if logprob is not None:
            probs.append(math.exp(min(logprob, 0.0)))
    return round(sum(probs) / len(probs), 4) if probs else None
