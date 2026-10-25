"""Speech-to-text interface (ARCHITECTURE section 3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

UNKNOWN_LANG = "unknown"
_KNOWN_LANGS = ("ru", "en")
_LANG_NAMES = {"russian": "ru", "english": "en"}


def normalize_lang(lang: str | None) -> str:
    """Map a provider's language code ("ru", "en-US", "Russian"...) to "ru" / "en" / "unknown"."""
    if not lang:
        return UNKNOWN_LANG
    code = lang.strip().lower().replace("_", "-").split("-")[0]
    code = _LANG_NAMES.get(code, code)
    return code if code in _KNOWN_LANGS else UNKNOWN_LANG


@dataclass(frozen=True)
class Transcript:
    """Result of one recognition. ``text == ""`` means "nothing was understood", not an error."""

    text: str
    lang: str = UNKNOWN_LANG  # "ru" | "en" | "unknown"
    confidence: float | None = None  # 0..1 when the backend reports it
    provider: str = ""  # name of the provider that produced it (set by STTChain)
    duration_s: float | None = None  # length of the recognised audio (set by STTChain)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@runtime_checkable
class STTProvider(Protocol):
    name: str

    def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = 16_000,
        *,
        lang_hint: str | None = None,
        prompt: str | None = None,
        timeout_s: float,
    ) -> Transcript:
        """Recognise mono int16 PCM.

        Raises ``RateLimited`` / ``ProviderUnavailable`` / ``ProviderRequestError`` (all
        ``ProviderError``). Silence or noise yields ``Transcript("")``, not an exception.
        """
        ...
