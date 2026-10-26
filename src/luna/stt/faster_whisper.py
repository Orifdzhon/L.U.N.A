"""Local speech-to-text with faster-whisper (CTranslate2); the offline STT fallback.

Defaults are safe for the reference machine: CPU, ``int8``, ``base``, beam 1. CUDA is opt-in
(``device="cuda"`` or ``"auto"``) because the GPU is usually busy with the local LLM.

Heavy imports (``faster_whisper``, ``ctranslate2``, ``numpy``) happen lazily, never at import time
of this module. The model is loaded once (first ``transcribe()`` or ``warm_up()``), under a lock;
a failed load leaves nothing behind, so the next call simply tries again (the ``STTChain`` cooldown
keeps that from happening on every request). The only network access is the one-time model
download done by ``faster-whisper`` itself.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from luna.core.errors import ProviderRequestError, ProviderUnavailable
from luna.stt.base import UNKNOWN_LANG, Transcript, normalize_lang

_log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # faster-whisper expects 16 kHz mono float32
_MAX_AUDIO_S = 120.0

# Approximate download sizes, for the "downloading ..." log line only.
_MODEL_SIZE_MB = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 145,
    "base.en": 145,
    "small": 465,
    "small.en": 465,
    "medium": 1500,
    "medium.en": 1500,
    "large-v2": 3000,
    "large-v3": 3000,
    "large-v3-turbo": 1600,
    "turbo": 1600,
}

ModelFactory = Callable[..., Any]
CudaProbe = Callable[[], int]


def default_model_dir(env: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\LUNA\\models`` (falls back to ``~/AppData/Local`` if unset)."""
    env = os.environ if env is None else env
    base = env.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "LUNA" / "models"


def add_nvidia_dll_directories(
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
    add_dll_directory: Callable[[str], Any] | None = None,
) -> list[str]:
    """Make pip-installed CUDA 12 libs (``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12``) loadable.

    Adds each package's ``bin`` folder via ``os.add_dll_directory`` (Windows only). Returns the
    folders added; nothing is added when the packages are absent.
    """
    if add_dll_directory is None:
        add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return []
    added: list[str] = []
    for package in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = find_spec(package)
        except (ImportError, ValueError):
            continue
        if spec is None:
            continue
        for location in spec.submodule_search_locations or []:
            bin_dir = Path(location) / "bin"
            if bin_dir.is_dir():
                add_dll_directory(str(bin_dir))
                added.append(str(bin_dir))
    return added


def _default_cuda_probe() -> int:
    import ctranslate2

    return int(ctranslate2.get_cuda_device_count())


def _default_factory(*args: Any, **kwargs: Any) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(*args, **kwargs)


class FasterWhisperSTT:
    name = "local"

    def __init__(
        self,
        model: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        model_dir: Path | str | None = None,
        *,
        vad_filter: bool = False,
        model_factory: ModelFactory | None = None,
        cuda_probe: CudaProbe | None = None,
        dll_setup: Callable[[], list[str]] = add_nvidia_dll_directories,
    ) -> None:
        if device not in ("cpu", "cuda", "auto"):
            raise ValueError(f"device must be cpu, cuda or auto, got {device!r}")
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._beam_size = beam_size
        self._model_dir = Path(model_dir) if model_dir else default_model_dir()
        self._vad_filter = vad_filter
        self._factory = model_factory
        self._cuda_probe = cuda_probe
        self._dll_setup = dll_setup
        self._load_lock = threading.Lock()
        self._decode_lock = threading.Lock()
        self._model: Any = None
        self.resolved_device: str | None = None  # set after a successful load

    # -- loading -----------------------------------------------------------------------------

    def warm_up(self) -> None:
        """Load the model now (blocking). Raises ``ProviderUnavailable`` on failure."""
        self._ensure_model()

    def start_warm_up(self) -> threading.Thread:
        """Load the model in a daemon thread; failures are logged, not raised."""

        def run() -> None:
            try:
                self.warm_up()
            except ProviderUnavailable as exc:
                _log.warning("STT warm-up failed: %s", exc)

        thread = threading.Thread(target=run, name="stt-warm-up", daemon=True)
        thread.start()
        return thread

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _ensure_model(self) -> Any:
        model = self._model
        if model is not None:
            return model
        with self._load_lock:
            if self._model is None:
                self._model = self._load()
            return self._model

    def _load(self) -> Any:
        factory = self._factory
        if factory is None:
            if importlib.util.find_spec("faster_whisper") is None:
                raise ProviderUnavailable(
                    "faster-whisper is not installed; run `uv sync` (it is a core dependency)"
                )
            factory = _default_factory
        device = self._choose_device()
        self._log_download_if_needed()
        compute_type = self._compute_type
        try:
            model = self._build(factory, device, compute_type)
        except Exception as exc:
            if device == "cuda" and self._device == "cuda":
                raise self._cuda_error(exc) from exc
            if self._device != "auto" or device != "cuda":
                raise self._load_error(exc) from exc
            _log.warning(
                "CUDA model load failed (%s: %s); falling back to CPU", type(exc).__name__, exc
            )
            device = "cpu"
            if "float16" in compute_type:
                compute_type = "int8"
            try:
                model = self._build(factory, device, compute_type)
            except Exception as exc2:
                raise self._load_error(exc2) from exc2
        self.resolved_device = device
        _log.info("STT model %r ready on %s (%s)", self._model_name, device, compute_type)
        return model

    def _build(self, factory: ModelFactory, device: str, compute_type: str) -> Any:
        self._model_dir.mkdir(parents=True, exist_ok=True)
        _log.info("loading STT model %r on %s (%s)", self._model_name, device, compute_type)
        model = factory(
            self._model_name,
            device=device,
            compute_type=compute_type,
            download_root=str(self._model_dir),
        )
        if device == "cuda":
            # Loading succeeds even when cuBLAS/cuDNN are missing; they are only needed when
            # decoding. Prove CUDA works now so a broken GPU setup falls back before real audio.
            import numpy as np

            segments, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size=1)
            list(segments)
        return model

    def _cuda_error(self, exc: Exception) -> ProviderUnavailable:
        return ProviderUnavailable(
            f"CUDA is configured for local STT but failed ({type(exc).__name__}: {exc}); "
            "CUDA 12 cuBLAS/cuDNN 9 libraries are probably missing. "
            "Set [stt.local] device = 'cpu' or 'auto'"
        )

    def _load_error(self, exc: Exception) -> ProviderUnavailable:
        return ProviderUnavailable(
            f"could not load the local STT model {self._model_name!r} "
            f"({type(exc).__name__}). The first use downloads it into {self._model_dir}: "
            "check the internet connection and free disk space"
        )

    def _choose_device(self) -> str:
        if self._device == "cpu":
            return "cpu"
        count = 0
        error: Exception | None = None
        try:
            if self._cuda_probe is None and sys.platform == "win32":
                self._dll_setup()
            count = (self._cuda_probe or _default_cuda_probe)()
        except Exception as exc:  # ctranslate2 missing / CUDA driver trouble
            error = exc
        if count > 0:
            return "cuda"
        reason = f"{type(error).__name__}" if error else "no CUDA device found"
        if self._device == "cuda":
            raise ProviderUnavailable(
                f"device='cuda' is configured but CUDA is not available ({reason}); "
                "set [stt.local] device = 'cpu' or 'auto'"
            )
        _log.warning("CUDA is not available (%s); using CPU for local STT", reason)
        return "cpu"

    def _log_download_if_needed(self) -> None:
        name = self._model_name
        if Path(name).exists() or "/" in name:
            return
        if any(self._model_dir.glob(f"models--*faster-whisper-{name}")):
            return
        size = _MODEL_SIZE_MB.get(name)
        about = f", about {size} MB" if size else ""
        _log.info("downloading STT model %r%s (first use) into %s", name, about, self._model_dir)

    # -- recognition -------------------------------------------------------------------------

    def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = SAMPLE_RATE,
        *,
        lang_hint: str | None = None,
        prompt: str | None = None,
        timeout_s: float,
    ) -> Transcript:
        """Recognise mono int16 PCM at 16 kHz.

        ``timeout_s`` is not enforced: CPU decoding cannot be interrupted safely; short voice
        commands decode in about a second.
        """
        if sample_rate != SAMPLE_RATE:
            raise ProviderRequestError(f"local STT needs {SAMPLE_RATE} Hz audio, got {sample_rate}")
        if len(pcm16) % 2:
            raise ProviderRequestError("pcm16 must hold whole int16 samples (even byte length)")
        if not pcm16:
            raise ProviderRequestError("no audio to transcribe")
        if len(pcm16) / 2 / SAMPLE_RATE > _MAX_AUDIO_S:
            raise ProviderRequestError(f"audio longer than {_MAX_AUDIO_S:.0f} s")

        import numpy as np

        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        language = normalize_lang(lang_hint) if lang_hint else None
        if language == UNKNOWN_LANG:
            language = None

        model = self._ensure_model()
        try:
            with self._decode_lock:
                segments, info = model.transcribe(
                    audio,
                    language=language,
                    initial_prompt=prompt or None,
                    vad_filter=self._vad_filter,
                    beam_size=self._beam_size,
                    condition_on_previous_text=False,
                )
                kept = [s for s in segments if not _is_silence(s)]
        except Exception as exc:
            raise ProviderUnavailable(f"local STT decoding failed ({type(exc).__name__})") from exc

        text = " ".join(s.text.strip() for s in kept if s.text.strip()).strip()
        if not text:
            return Transcript("")
        return Transcript(
            text=text,
            lang=normalize_lang(getattr(info, "language", None)),
            confidence=_confidence(kept, getattr(info, "language_probability", None)),
        )

    def transcribe_short(
        self, pcm16: bytes, *, language: str = "ru", max_new_tokens: int = 12
    ) -> Transcript:
        """Decode a short clip (wake verification): same model and decode lock, beam 1.

        No ``initial_prompt`` and no ``hotwords`` (biasing would defeat the check), no language
        detection pass, no timestamps, at most ``max_new_tokens`` output tokens.
        """
        if len(pcm16) % 2:
            raise ProviderRequestError("pcm16 must hold whole int16 samples (even byte length)")
        if not pcm16:
            return Transcript("")

        import numpy as np

        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        model = self._ensure_model()
        try:
            with self._decode_lock:
                segments, info = model.transcribe(
                    audio,
                    language=language,
                    beam_size=1,
                    without_timestamps=True,
                    max_new_tokens=max_new_tokens,
                    condition_on_previous_text=False,
                    vad_filter=False,
                )
                kept = [s for s in segments if not _is_silence(s)]
        except Exception as exc:
            raise ProviderUnavailable(f"local STT decoding failed ({type(exc).__name__})") from exc

        text = " ".join(s.text.strip() for s in kept if s.text.strip()).strip()
        if not text:
            return Transcript("")
        return Transcript(
            text=text,
            lang=normalize_lang(getattr(info, "language", None)),
            confidence=_confidence(kept, getattr(info, "language_probability", None)),
        )


def _is_silence(segment: Any) -> bool:
    """Whisper's own rule for hallucinated text over silence."""
    return (
        getattr(segment, "no_speech_prob", 0.0) > 0.6
        and getattr(segment, "avg_logprob", 0.0) < -1.0
    )


def _confidence(segments: list[Any], language_probability: float | None) -> float | None:
    logprobs = [
        s.avg_logprob for s in segments if isinstance(getattr(s, "avg_logprob", None), float)
    ]
    if logprobs:
        return max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs))))
    if isinstance(language_probability, float):
        return max(0.0, min(1.0, language_probability))
    return None
