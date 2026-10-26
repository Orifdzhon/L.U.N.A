"""``PiperTTS``: local neural voices (Piper VITS models) run by sherpa-onnx (ADR 0005,
amendment 3).

No network at synthesis time and no stalls: on the owner's CPU the first audio of a short
sentence takes 0.1-0.25 s after warm-up. The models (one per language, about 65 MB each) are
downloaded once by :class:`~luna.tts.piper_model.PiperModelStore`.

- ``sherpa_onnx`` is imported lazily, each language's model is loaded lazily and once (a second
  thread asking for the same language waits for the first load);
- a failed load or download is remembered for ``retry_after_s`` so that the fallback voice speaks
  at once instead of every chunk waiting for another failing download;
- ``rate`` (-10..10) maps to the model speed (5 % per step), ``volume`` (0..100) is a linear gain
  applied to the samples;
- text is normalised first (numbers, currency, Latin words, see ``luna.tts.normalize``).

The engine and the model store are injectable, so unit tests never load a model.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from luna.audio.base import AudioClip
from luna.core.errors import NothingToSpeak, ProviderRequestError, ProviderUnavailable
from luna.tts.normalize import normalize_for_speech
from luna.tts.piper_model import VOICES, PiperModelStore, VoiceSpec
from luna.tts.text import is_speakable

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000
WARM_UP_TEXT = "Да"
DEFAULT_THREADS = 2
RETRY_AFTER_S = 300.0


class Engine(Protocol):
    """The part of ``sherpa_onnx.OfflineTts`` that is used."""

    def generate(self, text: str, sid: int = 0, speed: float = 1.0) -> Any:
        """Return an object with ``samples`` (float -1..1) and ``sample_rate``."""
        ...


# (voice directory, voice spec, threads) -> engine. Faked in unit tests.
EngineFactory = Callable[[Path, VoiceSpec, int], Engine]


def rate_to_speed(rate: int) -> float:
    """Map the SAPI-style ``rate`` -10..10 to a model speed: 5 % per step (0.5 .. 1.5)."""
    return 1.0 + max(-10, min(10, int(rate))) * 0.05


def volume_to_gain(volume: int) -> float:
    """Map ``volume`` 0..100 to a linear gain 0.0 .. 1.0."""
    return max(0, min(100, int(volume))) / 100.0


def _sherpa_engine(path: Path, spec: VoiceSpec, num_threads: int) -> Engine:
    try:
        import sherpa_onnx
    except ImportError as exc:  # also a DLL load failure
        raise ProviderUnavailable(f"sherpa-onnx cannot be loaded ({type(exc).__name__})") from exc
    vits = sherpa_onnx.OfflineTtsVitsModelConfig(
        model=str(path / spec.model_file),
        tokens=str(path / "tokens.txt"),
        data_dir=str(path / "espeak-ng-data"),
    )
    model = sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=num_threads, provider="cpu")
    config = sherpa_onnx.OfflineTtsConfig(model=model, max_num_sentences=1)
    engine: Engine = sherpa_onnx.OfflineTts(config)
    return engine


def _to_pcm(samples: Any, gain: float) -> bytes:
    import numpy as np

    data = np.asarray(samples, dtype=np.float32)
    if gain != 1.0:
        data = data * np.float32(gain)
    return bytes(np.clip(data * 32767.0, -32768.0, 32767.0).astype("<i2").tobytes())


class _Loaded:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.lock = threading.Lock()  # one generate() at a time per model


class PiperTTS:
    name = "piper"

    def __init__(
        self,
        voice_ru: str = "ru_RU-irina-medium",
        voice_en: str = "en_US-kristin-medium",
        rate: int = 0,
        volume: int = 100,
        *,
        model_dir: Path | str | None = None,
        num_threads: int = DEFAULT_THREADS,
        store: PiperModelStore | None = None,
        engine_factory: EngineFactory | None = None,
        retry_after_s: float = RETRY_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._voices: dict[str, VoiceSpec] = {}
        for lang, key in (("ru", voice_ru), ("en", voice_en)):
            spec = VOICES.get(key)
            if spec is None or spec.lang != lang:
                raise ValueError(f"not a known Piper {lang} voice: {key!r}")
            self._voices[lang] = spec
        self._speed = rate_to_speed(rate)
        self._gain = volume_to_gain(volume)
        self._threads = max(1, int(num_threads))
        self._store = store or PiperModelStore(model_dir)
        self._factory = engine_factory or _sherpa_engine
        self._retry_after_s = retry_after_s
        self._clock = clock
        self._lock = threading.Lock()  # guards the dicts below
        self._loaded: dict[str, _Loaded] = {}
        self._load_locks = {lang: threading.Lock() for lang in self._voices}
        self._failed: dict[str, tuple[float, str]] = {}
        self._warmed = False

    # -- loading -----------------------------------------------------------------------------

    def _engine(self, lang: str) -> _Loaded:
        with self._lock:
            loaded = self._loaded.get(lang)
            failed = self._failed.get(lang)
        if loaded is not None:
            return loaded
        if failed is not None and self._clock() < failed[0]:
            raise ProviderUnavailable(failed[1])
        with self._load_locks[lang]:
            with self._lock:
                loaded = self._loaded.get(lang)
            if loaded is not None:
                return loaded
            spec = self._voices[lang]
            try:
                path = self._store.ensure(spec.key)
                engine = self._factory(path, spec, self._threads)
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, ProviderUnavailable)
                    else f"cannot load the Piper voice {spec.key} ({type(exc).__name__})"
                )
                with self._lock:
                    self._failed[lang] = (self._clock() + self._retry_after_s, message)
                log.warning("local voice %s unavailable: %s", spec.key, message)
                if isinstance(exc, ProviderUnavailable):
                    raise
                raise ProviderUnavailable(message) from exc
            loaded = _Loaded(engine)
            with self._lock:
                self._loaded[lang] = loaded
                self._failed.pop(lang, None)
            return loaded

    # -- API ---------------------------------------------------------------------------------

    def warm_up(self) -> None:
        """Load the Russian voice (downloading it on first use) and synthesize one tiny word.

        Blocking; the Speaker calls it from a background thread. Never raises, and does nothing
        once it has succeeded (a local model does not go cold, unlike an online service).
        """
        if self._warmed:
            return
        started = time.monotonic()
        try:
            loaded = self._engine("ru")
            with loaded.lock:
                loaded.engine.generate(WARM_UP_TEXT, sid=0, speed=self._speed)
        except Exception as e:
            log.debug("Piper warm-up failed (%s)", type(e).__name__, exc_info=True)
            return
        self._warmed = True
        log.debug("Piper warm-up took %.0f ms", (time.monotonic() - started) * 1000)

    def synthesize(self, text: str, lang: str) -> AudioClip:
        if not text or not text.strip():
            raise ProviderRequestError("nothing to speak: empty text")
        if len(text) > MAX_TEXT_CHARS:
            raise ProviderRequestError(f"text too long to speak ({len(text)} > {MAX_TEXT_CHARS})")
        key = lang.strip().lower()
        if key not in self._voices:
            raise ProviderRequestError(f"unsupported language: {lang!r}")
        clean = text.encode("utf-8", "replace").decode("utf-8")
        if not is_speakable(clean):
            raise NothingToSpeak("no character the Piper voice can pronounce")
        spoken = normalize_for_speech(clean, key)
        if not is_speakable(spoken):
            raise NothingToSpeak("no character the Piper voice can pronounce")
        loaded = self._engine(key)
        try:
            with loaded.lock:
                audio = loaded.engine.generate(spoken, sid=0, speed=self._speed)
            pcm = _to_pcm(audio.samples, self._gain)
            rate = int(audio.sample_rate)
        except Exception as e:
            raise ProviderUnavailable(f"Piper synthesis failed ({type(e).__name__})") from e
        if not pcm:
            raise ProviderUnavailable("Piper returned no audio")
        return AudioClip(pcm=pcm, sample_rate=rate)
