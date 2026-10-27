"""Vosk keyword spotter (ADR 0004, ARCHITECTURE section 3).

A ``KaldiRecognizer`` fed 30 ms PCM16 frames (exactly ``frame_samples`` = 480 samples; the caller,
#108, must re-chunk the mic stream to that size). Modes:

- ``"grammar"`` (default): grammar restricted to the keywords plus ``[unk]`` (every sound is
  forced to "keyword" or "unknown").
- ``"free"``: no grammar (the full small-ru language model); a keyword counts when it appears as a
  whole recognized word.

A keyword is reported once, when the number of accepted keyword words in the utterance grows.
Every path applies the same filters: word ``conf`` >= ``threshold`` (a word without ``conf`` never
fires) and word duration >= ``min_word_s`` (0 disables; measured: a false "луна" lasted 0.24 s,
real ones 0.36-0.47 s). Partial results are used when ``report_partials`` is true (low latency,
needed for one-breath commands); with it off, only final results (``AcceptWaveform`` returned
true) report. A final result is also a flush: it reports a keyword the partials missed and starts
a new utterance. Measured with vosk 0.3.45 and
small-ru-0.22: the keyword shows up in the *final* result (about 0.6 s after the word ends),
final results carry ``conf``, and word times are cumulative from the recognizer's start.

``import vosk`` happens lazily in the constructor (``vosk`` is a core dependency but a native
library; a broken install must degrade wake mode, not the whole app). Vosk logging is silenced.

The constructor loads the model synchronously (about a second, 45 MB): construct it off the
asyncio loop. Not thread-safe (see ``luna.wakeword.base``). The model is loaded once;
``reset()`` builds a fresh recognizer from it, so word timings (and thus ``end_sample``) restart
at zero.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from luna.core.errors import ConfigError, ProviderUnavailable
from luna.wakeword.base import WAKE_SAMPLE_RATE, WAKE_SAMPLE_WIDTH_BYTES, WakeEvent

log = logging.getLogger(__name__)

FRAME_MS = 30
FRAME_SAMPLES = WAKE_SAMPLE_RATE * FRAME_MS // 1000  # 480
DEFAULT_MIN_WORD_S = 0.30
UNKNOWN_TOKEN = "[unk]"
FALLBACK_LAG_MS = 250  # cut used when the engine reports no word timings (ARCHITECTURE section 3)


class Recognizer(Protocol):
    """The subset of ``vosk.KaldiRecognizer`` used here."""

    def AcceptWaveform(self, data: bytes) -> bool: ...
    def Result(self) -> str: ...
    def FinalResult(self) -> str: ...
    def PartialResult(self) -> str: ...


class Engine(Protocol):
    """Loaded model + recognizer factory (the real one wraps ``vosk``; tests inject a fake)."""

    def has_word(self, word: str) -> bool: ...
    def create(self, grammar: str | None, sample_rate: int) -> Recognizer: ...
    def close(self) -> None: ...


class _VoskEngine:
    def __init__(self, model_path: Path) -> None:
        try:
            import vosk
        except (ImportError, OSError) as exc:  # OSError: the native library did not load
            raise ProviderUnavailable(
                "the 'vosk' package is not usable "
                f"({type(exc).__name__}); run 'uv sync' to install the project dependencies"
            ) from exc
        self._vosk = vosk
        vosk.SetLogLevel(-1)
        try:
            self._model: Any = vosk.Model(model_path=str(model_path))
        except Exception as exc:
            raise ProviderUnavailable(
                f"cannot load the wake-word model from {model_path}: delete the folder to "
                "download it again"
            ) from exc

    def has_word(self, word: str) -> bool:
        find = getattr(self._model, "vosk_model_find_word", None)
        if find is None:
            raise ConfigError(
                "this vosk build cannot check the model vocabulary (vosk_model_find_word is "
                "missing); upgrade vosk"
            )
        return bool(find(word) >= 0)

    def create(self, grammar: str | None, sample_rate: int) -> Recognizer:
        args = (
            (self._model, sample_rate) if grammar is None else (self._model, sample_rate, grammar)
        )
        rec = self._vosk.KaldiRecognizer(*args)
        rec.SetWords(True)
        set_partial = getattr(rec, "SetPartialWords", None)
        if set_partial is not None:
            set_partial(True)
        return rec

    def close(self) -> None:
        self._model = None


EngineFactory = Callable[[Path], Engine]


class VoskKeywordSpotter:
    name = "vosk"

    def __init__(
        self,
        model_path: Path | str,
        keywords: Sequence[str],
        *,
        threshold: float,
        sample_rate: int = WAKE_SAMPLE_RATE,
        frame_samples: int = FRAME_SAMPLES,
        mode: Literal["grammar", "free"] = "grammar",
        min_word_s: float = DEFAULT_MIN_WORD_S,
        report_partials: bool = True,
        engine_factory: EngineFactory = _VoskEngine,
    ) -> None:
        words = [w.strip().lower() for w in keywords if w.strip()]
        if not words:
            raise ConfigError("wake-word keywords must not be empty")
        if not 0.0 <= threshold <= 1.0:
            raise ConfigError("wake-word threshold must be within 0..1")
        if sample_rate != WAKE_SAMPLE_RATE:
            raise ConfigError(f"wake word needs {WAKE_SAMPLE_RATE} Hz audio, got {sample_rate}")
        if frame_samples != FRAME_SAMPLES:
            raise ConfigError(f"frame_samples must be {FRAME_SAMPLES}, got {frame_samples}")
        if mode not in ("grammar", "free"):
            raise ConfigError(f"mode must be 'grammar' or 'free', got {mode!r}")
        if min_word_s < 0:
            raise ConfigError("min_word_s must be >= 0")
        self._min_word_s = min_word_s
        self._report_partials = report_partials
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._keywords = frozenset(words)
        self._threshold = threshold
        self._grammar: str | None = (
            json.dumps([*dict.fromkeys(words), UNKNOWN_TOKEN], ensure_ascii=False)
            if mode == "grammar"
            else None
        )
        self._engine: Engine | None = engine_factory(Path(model_path))
        try:
            missing = [w for w in dict.fromkeys(words) if not self._engine.has_word(w)]
            if missing:
                raise ConfigError(
                    f"wake-word keyword(s) {missing} are not in the model vocabulary; "
                    "choose common Russian words"
                )
            self._rec: Recognizer | None = self._engine.create(self._grammar, sample_rate)
        except BaseException:
            self._engine.close()
            self._engine = None
            raise
        self._fed = 0  # samples passed to process() since construction / reset()
        self._seen = 0  # keyword words of the current utterance already handled

    # -- WakeWordDetector --------------------------------------------------------------------

    def process(self, frame: bytes) -> WakeEvent | None:
        if self._rec is None:
            raise RuntimeError("VoskKeywordSpotter is closed")
        if len(frame) != self.frame_samples * WAKE_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                f"expected {self.frame_samples * WAKE_SAMPLE_WIDTH_BYTES} bytes, got {len(frame)}"
            )
        self._fed += self.frame_samples
        if self._rec.AcceptWaveform(frame):
            event = self._scan(self._payload(self._rec.Result()), "result")
            self._seen = 0  # the utterance ended: the next one starts counting from zero
            return event
        if not self._report_partials:
            return None
        return self._scan(self._payload(self._rec.PartialResult()), "partial_result")

    def flush(self) -> WakeEvent | None:
        """Scan the recognizer's final result (the keyword often only appears there)."""
        if self._rec is None:
            raise RuntimeError("VoskKeywordSpotter is closed")
        event = self._scan(self._payload(self._rec.FinalResult()), "result")
        self._seen = 0
        return event

    def reset(self) -> None:
        if self._engine is None:
            raise RuntimeError("VoskKeywordSpotter is closed")
        self._rec = self._engine.create(self._grammar, self.sample_rate)
        self._fed = 0
        self._seen = 0

    def close(self) -> None:
        if self._engine is not None:
            self._engine.close()
        self._engine = None
        self._rec = None

    # -- internals ---------------------------------------------------------------------------

    @staticmethod
    def _payload(raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _scan(self, data: dict[str, Any], words_key: str) -> WakeEvent | None:
        words = data.get(words_key)
        if not isinstance(words, list):
            return None
        hits = [w for w in words if isinstance(w, dict) and self._is_keyword(w)]
        # Report at most one keyword per call; a rejected word is looked at again next time
        # (a partial's confidence and timing can still change), so only accepted ones advance.
        for index in range(self._seen, len(hits)):
            word = hits[index]
            confidence = self._confidence(word)
            if confidence is None or confidence < self._threshold:
                log.debug("wake: %r rejected (confidence %s)", word.get("word"), confidence)
                continue
            duration = self._duration(word)
            if duration is not None and duration < self._min_word_s:
                log.debug("wake: %r rejected (%.2f s)", word.get("word"), duration)
                continue
            self._seen = index + 1
            return WakeEvent(str(word["word"]).lower(), confidence, self._end_sample(word))
        return None

    def _is_keyword(self, word: dict[str, Any]) -> bool:
        text = word.get("word")
        return isinstance(text, str) and text.lower() in self._keywords

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    def _confidence(self, word: dict[str, Any]) -> float | None:
        value = self._number(word.get("conf"))
        return None if value is None else min(1.0, max(0.0, value))

    def _duration(self, word: dict[str, Any]) -> float | None:
        start, end = self._number(word.get("start")), self._number(word.get("end"))
        return None if start is None or end is None else end - start

    def _end_sample(self, word: dict[str, Any]) -> int:
        end = self._number(word.get("end"))
        if end is not None and end >= 0:
            return min(self._fed, round(end * self.sample_rate))
        return max(0, self._fed - self.sample_rate * FALLBACK_LAG_MS // 1000)
