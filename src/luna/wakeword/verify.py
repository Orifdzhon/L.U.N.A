"""Second-stage wake verification (ADR 0004 amendment 2, D15; ARCHITECTURE section 3).

The cheap first stage (Vosk grammar) fires on near-homophones. The verifier decodes the short
window around the detection with the *shared* local faster-whisper model and accepts only if the
transcript starts with the address word ("луна" / "luna"). It is local only: a rejected wake never
leaves the machine. A verifier error fails open (``accepted=True``, ``reason="error"``) so wake
mode degrades to the first-stage-only baseline instead of going deaf.

No ``faster_whisper`` import here; the model lives behind ``FasterWhisperSTT``.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from luna.stt.faster_whisper import SAMPLE_RATE, FasterWhisperSTT

_log = logging.getLogger(__name__)

VERIFY_LEAD_MS = 1000  # end of lead_in (the keyword) ...
VERIFY_AFTER_MS = 700  # ... plus this much audio after the cut -> ~1.7 s window
VERIFY_TIMEOUT_S = 3.0  # no result by then -> fail open (reason "timeout"; enforced by the caller)

_SAMPLE_WIDTH = 2
_WARM_UP_SILENCE_S = 0.5
_YO_TO_IE = {0x0451: 0x0435}  # yo to ie
# Latin lookalike letters Whisper sometimes mixes into a Russian word.
_LATIN_TO_CYRILLIC = str.maketrans("aceopxykmthb", "асеорхукмтнв")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")  # the Cyrillic block
_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")  # letter runs with inner hyphens

Clock = Callable[[], float]
Reason = Literal["keyword", "no_keyword", "empty", "off", "error", "timeout"]


@dataclass(frozen=True)
class VerifyResult:
    accepted: bool
    reason: Reason
    text: str = ""  # what the verifier heard; log at DEBUG only
    latency_s: float = 0.0  # decode wall time


@runtime_checkable
class WakeVerifier(Protocol):
    name: str  # "local" | "off"

    def warm_up(self) -> None:
        """Blocking; raises ``ProviderUnavailable`` if the verifier cannot be used."""
        ...

    def verify(self, pcm16: bytes) -> VerifyResult:
        """16 kHz mono PCM16. A decode error gives ``VerifyResult(True, "error")`` (fail open)."""
        ...

    def close(self) -> None: ...


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower().translate(_YO_TO_IE)


def keyword_first(text: str, words: Sequence[str]) -> bool:
    """True iff the first token of ``text`` is exactly one of ``words`` (no fuzzy matching)."""
    match = _TOKEN.search(_normalize(text))
    if match is None:
        return False
    token = match.group(0)
    if _CYRILLIC.search(token):  # a mixed-script token: read the Latin lookalikes as Cyrillic
        token = token.translate(_LATIN_TO_CYRILLIC)
    return token in {_normalize(w).strip() for w in words}


def _ms_bytes(ms: int) -> int:
    return SAMPLE_RATE * ms // 1000 * _SAMPLE_WIDTH


def verify_window(
    lead_in: bytes,
    after: Sequence[bytes],
    lead_ms: int = VERIFY_LEAD_MS,
    after_ms: int = VERIFY_AFTER_MS,
) -> bytes:
    """Last ``lead_ms`` of ``lead_in`` + first ``after_ms`` of the frames after the cut.

    Not padded: if less audio exists, the window is shorter.
    """
    lead_n = _ms_bytes(lead_ms)
    head = lead_in[-lead_n:] if lead_n else b""
    if len(head) % _SAMPLE_WIDTH:
        head = head[1:]
    tail = b"".join(after)[: _ms_bytes(after_ms)]
    return head + tail


class FasterWhisperVerifier:
    """Verifies with the STT provider's own model (one model in RAM, one decode lock)."""

    name = "local"

    def __init__(
        self,
        stt: FasterWhisperSTT,
        words: Sequence[str] = ("луна", "luna"),
        *,
        fail_open: bool = True,
        clock: Clock = time.perf_counter,
    ) -> None:
        self._stt = stt
        self._words = tuple(words)
        self._fail_open = fail_open
        self._clock = clock

    def warm_up(self) -> None:
        """Load the model (once) and decode silence once to prime CTranslate2."""
        self._stt.warm_up()
        silence = bytes(_ms_bytes(int(_WARM_UP_SILENCE_S * 1000)))
        self._stt.transcribe_short(silence, language="ru")

    def verify(self, pcm16: bytes) -> VerifyResult:
        start = self._clock()
        try:
            text = self._stt.transcribe_short(pcm16, language="ru").text
        except Exception as exc:  # fail open: never break wake mode
            latency = self._clock() - start
            _log.warning("wake verification failed (%s)", type(exc).__name__)
            return VerifyResult(self._fail_open, "error", "", latency)
        latency = self._clock() - start
        if not text.strip():
            return VerifyResult(False, "empty", "", latency)
        if keyword_first(text, self._words):
            return VerifyResult(True, "keyword", text, latency)
        return VerifyResult(False, "no_keyword", text, latency)

    def close(self) -> None:
        """No-op: the model belongs to the STT provider."""


class PassThroughVerifier:
    """Accepts everything (``verify = "off"`` or the degraded fallback)."""

    name = "off"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason  # why verification is unavailable, for the REPL message

    def warm_up(self) -> None:
        return None

    def verify(self, pcm16: bytes) -> VerifyResult:
        return VerifyResult(True, "off")

    def close(self) -> None:
        return None
