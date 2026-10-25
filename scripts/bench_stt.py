"""STT benchmark: latency, accuracy and memory of Groq Whisper and local faster-whisper (Issue #87).

Usage::

    uv run python scripts/bench_stt.py [--providers groq,local] [--local-models base,small]
                                       [--source synth-edge|synth-sapi|recorded]
                                       [--local-device cpu|cuda|auto] [--compute-type int8]
                                       [--runs 1] [--delay 1] [--out results.json] [--cuda-probe]
    uv run python scripts/bench_stt.py --record          # read the phrases into the microphone

The phrases (RU, EN, mixed, app / site names, numbers, one silent clip) live in
``benchmarks/stt_phrases.toml`` and double as the reference transcripts. Audio comes from

- ``synth-edge`` (default) / ``synth-sapi``: the phrases are synthesized by a TTS voice, so a run is
  reproducible, and cached as 16 kHz mono WAV files under ``benchmarks/stt_audio/<source>/``;
- ``recorded``: ``benchmarks/stt_audio/recorded/*.wav`` made with ``--record``.

The audio folder and ``benchmarks/*.json`` are gitignored: no audio and no keys are committed.
Keys are read by the normal settings loader and are never printed. Per provider / model the report
has load time, latency per clip (cold = first clip, warm = the rest), RTF (latency / audio length),
WER / CER against the reference (Levenshtein on normalised text), language-detection accuracy,
empty results, peak process RAM and (when ``nvidia-smi`` exists) GPU memory.

GPU memory procedure: run once with LM Studio stopped and once with the 9B model loaded; the
``GPU MB`` column is the delta of ``nvidia-smi --query-gpu=memory.used`` around the model load.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from luna.audio.base import AudioClip
from luna.config.loader import load_settings
from luna.config.settings import Settings
from luna.core.errors import ConfigError, ProviderError, ProviderUnavailable, RateLimited
from luna.stt.base import STTProvider, normalize_lang

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PHRASES = REPO / "benchmarks" / "stt_phrases.toml"
DEFAULT_AUDIO_DIR = REPO / "benchmarks" / "stt_audio"
SAMPLE_RATE = 16_000
CATEGORIES = ("app", "site", "search", "chat", "number", "silence")
SOURCES = ("synth-edge", "synth-sapi", "recorded")
SILENCE_S = 1.5
SYNTH_PAD_S = 0.3  # silence added before and after a synthesized phrase, like a real recording
CUDA_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")
TIMEOUT_S = 30.0


# -- phrase set --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Phrase:
    id: str
    lang: str  # ru | en | mixed
    category: str
    text: str

    @property
    def expected_lang(self) -> str:
        """Language the recogniser should report ("mixed" phrases are Russian sentences)."""
        return "en" if self.lang == "en" else "ru"

    @property
    def is_silence(self) -> bool:
        return not self.text.strip()


def load_phrases(path: Path) -> list[Phrase]:
    """Read the phrase file; raises ``ConfigError`` on a malformed or inconsistent file."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read phrases from {path}: {exc}") from exc
    phrases: list[Phrase] = []
    seen: set[str] = set()
    try:
        for raw in data.get("phrase", []):
            phrase = Phrase(raw["id"], raw["lang"], raw["category"], raw["text"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", phrase.id):
                raise ConfigError(f"phrase id {phrase.id!r} must be a safe file name")
            if phrase.id in seen:
                raise ConfigError(f"duplicate phrase id {phrase.id!r}")
            if phrase.lang not in ("ru", "en", "mixed"):
                raise ConfigError(f"phrase {phrase.id!r}: unknown lang {phrase.lang!r}")
            if phrase.category not in CATEGORIES:
                raise ConfigError(f"phrase {phrase.id!r}: unknown category {phrase.category!r}")
            if phrase.is_silence != (phrase.category == "silence"):
                raise ConfigError(f"phrase {phrase.id!r}: empty text <=> category 'silence'")
            seen.add(phrase.id)
            phrases.append(phrase)
    except KeyError as exc:
        raise ConfigError(f"phrase entry without field {exc} in {path}") from exc
    if not phrases:
        raise ConfigError(f"no phrases in {path}")
    return phrases


# -- text scoring ------------------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)


def normalize_text(text: str) -> str:
    """Casefold, yo -> ye (U+0451 -> U+0435), punctuation -> space, collapse whitespace.

    Digits are kept as they are.
    """
    folded = text.casefold().replace("ё", "е")  # noqa: RUF001 (Cyrillic on purpose)
    return " ".join(_PUNCT.sub(" ", folded).split())


def edit_distance(a: Sequence[Any], b: Sequence[Any]) -> int:
    """Levenshtein distance (substitution / insertion / deletion all cost 1)."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, item_a in enumerate(a, 1):
        current = [i]
        for j, item_b in enumerate(b, 1):
            cost = 0 if item_a == item_b else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class TextScore:
    word_edits: int
    ref_words: int
    char_edits: int
    ref_chars: int

    @property
    def wer(self) -> float | None:
        return self.word_edits / self.ref_words if self.ref_words else None

    @property
    def cer(self) -> float | None:
        return self.char_edits / self.ref_chars if self.ref_chars else None


def score_text(reference: str, hypothesis: str) -> TextScore:
    """Word and character edits of ``hypothesis`` against ``reference`` after normalisation."""
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    ref_words, hyp_words = ref.split(), hyp.split()
    return TextScore(
        edit_distance(ref_words, hyp_words), len(ref_words), edit_distance(ref, hyp), len(ref)
    )


# -- audio files -------------------------------------------------------------------------------


def write_wav(
    path: Path, pcm16: bytes, sample_rate: int = SAMPLE_RATE, *, exist_ok: bool = False
) -> None:
    """Write mono 16-bit WAV. Refuses to overwrite an existing file unless ``exist_ok``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb" if exist_ok else "xb") as fh:
        _write_wav_to(fh, pcm16, sample_rate)


def _write_wav_to(fh: BinaryIO, pcm16: bytes, sample_rate: int) -> None:
    with wave.open(fh, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16)


def resample_pcm16(pcm16: bytes, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    """Linear-interpolation resampling of mono int16 PCM (good enough for speech tests)."""
    if src_rate == dst_rate or not pcm16:
        return pcm16
    import numpy as np

    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
    count = max(1, round(len(samples) * dst_rate / src_rate))
    positions = np.linspace(0, len(samples) - 1, count)
    out = np.interp(positions, np.arange(len(samples)), samples)
    return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()


def read_wav(path: Path) -> bytes:
    """Read a 16-bit WAV as 16 kHz mono PCM (first channel kept, resampled when needed)."""
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: expected 16-bit audio, got {width * 8}-bit")
    if channels > 1:
        step = channels * width
        frames = b"".join(frames[i : i + width] for i in range(0, len(frames) - step + 1, step))
    return resample_pcm16(frames, rate, SAMPLE_RATE)


def silence_pcm(seconds: float) -> bytes:
    return bytes(int(seconds * SAMPLE_RATE) * 2)


@dataclass(frozen=True)
class Clip:
    phrase: Phrase
    pcm16: bytes

    @property
    def duration_s(self) -> float:
        return len(self.pcm16) / 2 / SAMPLE_RATE


class TTSLike(Protocol):
    name: str

    def synthesize(self, text: str, lang: str) -> AudioClip: ...


def synthesize_clips(
    tts: TTSLike,
    phrases: Sequence[Phrase],
    folder: Path,
    *,
    log: Callable[[str], None] = lambda _msg: None,
) -> list[Clip]:
    """Synthesize every phrase (cached as ``<id>.wav`` + ``<id>.txt``; re-made when text changes).

    A phrase that fails to synthesize is skipped with a log line; the run goes on.
    """
    clips: list[Clip] = []
    for phrase in phrases:
        wav_path, txt_path = folder / f"{phrase.id}.wav", folder / f"{phrase.id}.txt"
        if phrase.is_silence:
            clips.append(Clip(phrase, silence_pcm(SILENCE_S)))
            continue
        cached = (
            wav_path.is_file()
            and txt_path.is_file()
            and txt_path.read_text(encoding="utf-8") == phrase.text
        )
        if cached:
            clips.append(Clip(phrase, read_wav(wav_path)))
            continue
        try:
            audio = tts.synthesize(phrase.text, phrase.expected_lang)
        except ProviderError as exc:
            log(f"synthesis of {phrase.id} failed ({type(exc).__name__}): {exc}")
            continue
        pad = silence_pcm(SYNTH_PAD_S)
        pcm = pad + resample_pcm16(audio.pcm, audio.sample_rate) + pad
        write_wav(wav_path, pcm, exist_ok=True)
        txt_path.write_text(phrase.text, encoding="utf-8", newline="")
        clips.append(Clip(phrase, pcm))
    return clips


def load_recorded_clips(
    phrases: Sequence[Phrase], folder: Path, *, log: Callable[[str], None] = lambda _msg: None
) -> list[Clip]:
    """Clips recorded with ``--record``; phrases without a file are skipped (silence has none)."""
    clips: list[Clip] = []
    for phrase in phrases:
        path = folder / f"{phrase.id}.wav"
        if not path.is_file():
            if not phrase.is_silence:
                log(f"no recording for {phrase.id} ({path.name}); skipped")
            continue
        try:
            clips.append(Clip(phrase, read_wav(path)))
        except (wave.Error, EOFError, OSError, ValueError) as exc:
            log(f"cannot read {path.name}: {exc}")
    return clips


class UtteranceRecorder(Protocol):
    def record_pcm(self) -> bytes:
        """Block until one utterance is captured; empty bytes when nothing was heard."""
        ...


def record_phrases(
    phrases: Sequence[Phrase],
    folder: Path,
    recorder: UtteranceRecorder,
    *,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    """Prompt for each phrase and save ``<id>.wav`` (16 kHz mono). Existing files are never
    overwritten. Returns the number of files written. ``q`` at a prompt stops."""
    written = 0
    todo = [p for p in phrases if not p.is_silence]
    for index, phrase in enumerate(todo, 1):
        path = folder / f"{phrase.id}.wav"
        if path.exists():
            out(f"[{index}/{len(todo)}] {phrase.id}: {path.name} exists, not overwriting")
            continue
        while True:
            answer = (
                input_fn(
                    f"[{index}/{len(todo)}] Say: «{phrase.text}»  "
                    "(Enter = record, s = skip, q = quit) "
                )
                .strip()
                .lower()
            )
            if answer == "q":
                return written
            if answer == "s":
                break
            pcm = recorder.record_pcm()
            if not pcm:
                out("  nothing heard, try again")
                continue
            seconds = len(pcm) / 2 / SAMPLE_RATE
            verdict = (
                input_fn(f"  recorded {seconds:.1f} s. Enter = keep, r = redo ").strip().lower()
            )
            if verdict == "r":
                continue
            write_wav(path, pcm)
            written += 1
            break
    return written


class MicUtteranceRecorder:
    """Captures one utterance with the production microphone / VAD / recorder code."""

    def __init__(self, settings: Settings) -> None:
        from luna.audio.capture import SoundDeviceMic
        from luna.audio.recorder import Recorder
        from luna.audio.vad import EnergyVAD

        conf = settings.voice
        self._mic = SoundDeviceMic(conf.input_device, conf.frame_ms, conf.preroll_ms)
        vad = EnergyVAD(conf.vad_energy_threshold, conf.vad_noise_margin, conf.frame_ms)
        self._recorder = Recorder(self._mic, vad, conf)

    def record_pcm(self) -> bytes:
        return self._recorder.record(threading.Event()).pcm16

    def close(self) -> None:
        self._mic.close()


# -- results and aggregation -------------------------------------------------------------------


@dataclass
class ClipResult:
    clip_id: str
    lang: str
    category: str
    duration_s: float
    run: int = 1
    latency_s: float | None = None
    rtf: float | None = None
    text: str = ""
    detected_lang: str | None = None
    lang_ok: bool | None = None
    word_edits: int = 0
    ref_words: int = 0
    char_edits: int = 0
    ref_chars: int = 0
    empty: bool = False
    error: str | None = None  # exception class name only, never the message


@dataclass
class VariantReport:
    name: str  # groq | local
    model: str = ""
    device: str = ""
    skipped_reason: str | None = None
    stopped_reason: str | None = None
    load_s: float | None = None
    peak_rss_mb: float | None = None
    gpu_mb: float | None = None
    results: list[ClipResult] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name}/{self.model}" if self.model else self.name


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolation percentile; ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _rate(edits: int, total: int) -> float | None:
    return round(100 * edits / total, 1) if total else None


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def summarize(report: VariantReport) -> dict[str, Any]:
    done = [r for r in report.results if r.error is None]
    speech = [r for r in done if r.category != "silence"]
    silent = [r for r in done if r.category == "silence"]
    latencies = [r.latency_s for r in done if r.latency_s is not None]
    warm = [r.latency_s for r in done[1:] if r.latency_s is not None]
    rtfs = [r.rtf for r in done if r.rtf is not None]
    errors: dict[str, int] = {}
    for r in report.results:
        if r.error:
            errors[r.error] = errors.get(r.error, 0) + 1
    by_lang: dict[str, float | None] = {}
    for lang in ("ru", "en", "mixed"):
        group = [r for r in speech if r.lang == lang]
        by_lang[lang] = _rate(sum(r.word_edits for r in group), sum(r.ref_words for r in group))
    lang_checked = [r for r in speech if r.lang_ok is not None]
    return {
        "clips": len(report.results),
        "run": len(done),
        "errors": errors,
        "load_s": _round(report.load_s),
        "cold_s": _round(done[0].latency_s) if done else None,
        "warm_p50_s": _round(percentile(warm, 50)),
        "warm_p95_s": _round(percentile(warm, 95)),
        "max_s": _round(max(latencies) if latencies else None),
        "rtf_mean": _round(sum(rtfs) / len(rtfs) if rtfs else None, 3),
        "wer_pct": _rate(sum(r.word_edits for r in speech), sum(r.ref_words for r in speech)),
        "cer_pct": _rate(sum(r.char_edits for r in speech), sum(r.ref_chars for r in speech)),
        "wer_pct_by_lang": by_lang,
        "lang_accuracy_pct": _rate(sum(bool(r.lang_ok) for r in lang_checked), len(lang_checked)),
        "empty_results": sum(r.empty for r in speech),
        "silence_hallucinations": sum(not r.empty for r in silent),
        "peak_rss_mb": _round(report.peak_rss_mb, 0),
        "gpu_mb": _round(report.gpu_mb, 0),
    }


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def format_markdown(reports: Sequence[VariantReport], cuda: Mapping[str, Any] | None = None) -> str:
    """Summary table over all variants, one table per variant with the clips, optional CUDA part."""
    lines = [
        "## Summary",
        "",
        "| provider | model | device | load s | cold s | warm p50 s | warm p95 s | RTF "
        "| WER % | CER % | lang % | empty | silence text | RAM MB | GPU MB | errors | status |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        s = summarize(report)
        status = report.skipped_reason or report.stopped_reason or "ok"
        errs = ", ".join(f"{k} x{v}" for k, v in s["errors"].items()) or "-"
        lines.append(
            f"| {_cell(report.name)} | {_cell(report.model)} | {_cell(report.device or '-')} "
            f"| {_fmt(s['load_s'])} | {_fmt(s['cold_s'])} | {_fmt(s['warm_p50_s'])} "
            f"| {_fmt(s['warm_p95_s'])} | {_fmt(s['rtf_mean'], 3)} | {_fmt(s['wer_pct'], 1)} "
            f"| {_fmt(s['cer_pct'], 1)} | {_fmt(s['lang_accuracy_pct'], 1)} "
            f"| {s['empty_results']} | {s['silence_hallucinations']} "
            f"| {_fmt(s['peak_rss_mb'], 0)} | {_fmt(s['gpu_mb'], 0)} | {_cell(errs)} "
            f"| {_cell(status)} |"
        )
    for report in reports:
        if not report.results:
            continue
        lines += [
            "",
            f"## {report.label}",
            "",
            "| clip | lang | audio s | latency s | RTF | WER % | lang | text | error |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in report.results:
            wer = _rate(r.word_edits, r.ref_words) if r.category != "silence" else None
            lang_cell = (
                "-"
                if r.lang_ok is None
                else (f"{r.detected_lang}" if r.lang_ok else f"NO ({r.detected_lang})")
            )
            lines.append(
                f"| {_cell(r.clip_id)} | {r.lang} | {_fmt(r.duration_s)} | {_fmt(r.latency_s)} "
                f"| {_fmt(r.rtf, 3)} | {_fmt(wer, 1)} | {_cell(lang_cell)} "
                f"| {_cell(r.text or '-')} | {_cell(r.error or '-')} |"
            )
    if cuda is not None:
        lines += ["", "## CUDA probe", "", "```json", json.dumps(cuda, indent=2), "```"]
    return "\n".join(lines) + "\n"


def build_json(
    reports: Sequence[VariantReport],
    options: Mapping[str, Any],
    cuda: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "options": dict(options),
        "cuda": dict(cuda) if cuda is not None else None,
        "variants": [
            {
                "name": r.name,
                "model": r.model,
                "device": r.device,
                "skipped_reason": r.skipped_reason,
                "stopped_reason": r.stopped_reason,
                "summary": summarize(r),
                "results": [asdict(x) for x in r.results],
            }
            for r in reports
        ],
    }


# -- system probes -----------------------------------------------------------------------------


def peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB (``None`` when it cannot be read)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            windll = ctypes.WinDLL  # only reached on Windows
            kernel32, psapi = windll("kernel32"), windll("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(Counters),
                wintypes.DWORD,
            ]
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            if not psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                return None
            return counters.PeakWorkingSetSize / 1024 / 1024
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1024 / (1024 if sys.platform == "darwin" else 1)
    except Exception:
        return None


SmiRunner = Callable[[Sequence[str]], str]


def _run_smi(argv: Sequence[str]) -> str:
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=True)
    return done.stdout


def gpu_memory_mb(
    runner: SmiRunner = _run_smi, which: Callable[[str], str | None] = shutil.which
) -> float | None:
    """``memory.used`` of the first GPU in MB from ``nvidia-smi`` (``None`` when unavailable)."""
    exe = which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = runner([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
        return float(out.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def probe_cuda(
    *,
    device_count: Callable[[], int] | None = None,
    dll_loader: Callable[[str], Any] | None = None,
    dll_setup: Callable[[], list[str]] | None = None,
    smi: Callable[[], float | None] = gpu_memory_mb,
) -> dict[str, Any]:
    """Report whether CUDA 12 / cuDNN 9 can be used by faster-whisper. Never raises."""
    report: dict[str, Any] = {"platform": sys.platform}
    try:
        if dll_setup is None:
            from luna.stt.faster_whisper import add_nvidia_dll_directories as dll_setup
        report["pip_dll_dirs"] = dll_setup() if sys.platform == "win32" else []
    except Exception as exc:
        report["pip_dll_dirs"] = f"error: {type(exc).__name__}"
    try:
        if device_count is None:
            import ctranslate2

            device_count = ctranslate2.get_cuda_device_count
        report["ctranslate2_cuda_devices"] = int(device_count())
    except Exception as exc:
        report["ctranslate2_cuda_devices"] = f"error: {type(exc).__name__}"
    loader = dll_loader
    if loader is None and sys.platform == "win32":
        import ctypes

        loader = ctypes.WinDLL
    dlls: dict[str, bool | None] = {}
    for name in CUDA_DLLS:
        if loader is None:
            dlls[name] = None
            continue
        try:
            loader(name)
            dlls[name] = True
        except OSError:
            dlls[name] = False
    report["dlls_loadable"] = dlls
    report["gpu_memory_used_mb"] = smi()
    report["usable"] = (
        isinstance(report["ctranslate2_cuda_devices"], int)
        and report["ctranslate2_cuda_devices"] > 0
        and all(v is not False for v in dlls.values())
    )
    return report


# -- running -----------------------------------------------------------------------------------


def run_variant(
    report: VariantReport,
    provider: STTProvider,
    clips: Sequence[Clip],
    *,
    runs: int = 1,
    delay_s: float = 0.0,
    max_failures: int = 3,
    lang_hint: str = "auto",
    timeout_s: float = TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> VariantReport:
    """Transcribe every clip. ``max_failures`` consecutive ``RateLimited`` / unavailable results
    stop the variant (the remaining clips are not run). ``lang_hint``: auto | phrase."""
    failures = 0
    todo = [(run, clip) for run in range(1, runs + 1) for clip in clips]
    for index, (run, clip) in enumerate(todo):
        if index and delay_s > 0:
            sleep(delay_s)
        phrase = clip.phrase
        result = ClipResult(phrase.id, phrase.lang, phrase.category, clip.duration_s, run)
        hint = phrase.expected_lang if lang_hint == "phrase" else None
        started = clock()
        try:
            transcript = provider.transcribe(
                clip.pcm16, SAMPLE_RATE, lang_hint=hint, timeout_s=timeout_s
            )
        except ProviderError as exc:
            result.error = type(exc).__name__
            result.latency_s = clock() - started
            report.results.append(result)
            if isinstance(exc, (RateLimited, ProviderUnavailable)):
                failures += 1
                if failures >= max_failures:
                    report.stopped_reason = f"stopped after {failures} x {result.error}"
                    break
            else:
                failures = 0
            continue
        failures = 0
        result.latency_s = clock() - started
        result.rtf = result.latency_s / clip.duration_s if clip.duration_s else None
        result.text = transcript.text
        result.empty = transcript.is_empty
        if not phrase.is_silence:
            score = score_text(phrase.text, transcript.text)
            result.word_edits, result.ref_words = score.word_edits, score.ref_words
            result.char_edits, result.ref_chars = score.char_edits, score.ref_chars
            if not transcript.is_empty:
                result.detected_lang = normalize_lang(transcript.lang)
                result.lang_ok = result.detected_lang == phrase.expected_lang
        report.results.append(result)
    report.peak_rss_mb = peak_rss_mb()
    return report


# -- command line ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench_stt", description="L.U.N.A. STT benchmark")
    p.add_argument("--providers", default="groq,local", help="comma-separated: groq, local")
    p.add_argument("--local-models", help="comma-separated faster-whisper models (default: config)")
    p.add_argument("--local-device", choices=("cpu", "cuda", "auto"), default="cpu")
    p.add_argument("--compute-type", help="faster-whisper compute type (default: config)")
    p.add_argument(
        "--source", choices=SOURCES, default="synth-edge", help="where the audio is from"
    )
    p.add_argument(
        "--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR, help="clip cache / recordings"
    )
    p.add_argument("--phrases", type=Path, default=DEFAULT_PHRASES, help="phrase file (TOML)")
    p.add_argument("--limit", type=int, help="use only the first N phrases (quick smoke run)")
    p.add_argument("--record", action="store_true", help="record the phrases from the microphone")
    p.add_argument("--runs", type=int, default=1, help="repetitions of the clip set")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--max-failures", type=int, default=3, help="consecutive rate limits to stop")
    p.add_argument("--lang-hint", choices=("auto", "phrase"), default="auto")
    p.add_argument("--cuda-probe", action="store_true", help="report CUDA / cuDNN availability")
    p.add_argument("--out", type=Path, help="write the results as JSON to this file")
    p.add_argument("--config", type=Path, help="path to luna.toml")
    return p


def make_tts(source: str) -> TTSLike:
    if source == "synth-sapi":
        from luna.tts.sapi import SapiTTS

        return SapiTTS()
    from luna.tts.edge import EdgeTTS

    return EdgeTTS()


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _skip_reason(exc: Exception) -> str:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return first[:200]


def _bench_groq(
    settings: Settings, clips: Sequence[Clip], args: argparse.Namespace
) -> VariantReport:
    key = settings.api_key(settings.stt.groq.api_key_env)
    report = VariantReport("groq", settings.stt.groq.model, "cloud")
    if key is None:
        report.skipped_reason = f"no key ({settings.stt.groq.api_key_env})"
        return report
    from luna.stt.groq import GroqWhisperSTT

    provider = GroqWhisperSTT(key, settings.stt.groq.model)
    try:
        report.load_s = 0.0
        return run_variant(
            report, provider, clips, runs=args.runs, delay_s=args.delay,
            max_failures=args.max_failures, lang_hint=args.lang_hint,
        )  # fmt: skip
    finally:
        provider.close()


def _bench_local(
    settings: Settings, model: str, clips: Sequence[Clip], args: argparse.Namespace
) -> VariantReport:
    report = VariantReport("local", model, args.local_device)
    conf = settings.stt.local
    from luna.stt.faster_whisper import FasterWhisperSTT

    provider = FasterWhisperSTT(
        model,
        args.local_device,
        args.compute_type or conf.compute_type,
        conf.beam_size,
        conf.model_dir,
    )
    gpu_before = gpu_memory_mb()
    started = time.perf_counter()
    try:
        provider.warm_up()
    except ProviderUnavailable as exc:
        report.skipped_reason = _skip_reason(exc)
        return report
    report.load_s = time.perf_counter() - started
    report.device = provider.resolved_device or args.local_device
    gpu_after = gpu_memory_mb()
    if gpu_before is not None and gpu_after is not None:
        report.gpu_mb = gpu_after - gpu_before
    return run_variant(
        report, provider, clips, runs=args.runs, delay_s=0.0,
        max_failures=args.max_failures, lang_hint=args.lang_hint,
    )  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.runs < 1 or args.max_failures < 1 or args.delay < 0 or (args.limit or 1) < 1:
        _err("bench_stt: --runs, --max-failures and --limit must be >= 1, --delay >= 0")
        return 2
    try:
        from luna.ui.cli import configure_console_encoding

        configure_console_encoding()
        settings = load_settings(args.config)
        phrases = load_phrases(args.phrases)
    except (OSError, ConfigError) as exc:
        _err(f"bench_stt: {exc}")
        return 2
    logging.getLogger("luna").setLevel(logging.ERROR)
    if args.limit:
        phrases = phrases[: args.limit]

    if args.record:
        folder = args.audio_dir / "recorded"
        try:
            mic = MicUtteranceRecorder(settings)
        except Exception as exc:  # no sounddevice / no microphone
            _err(f"bench_stt: cannot open the microphone: {_skip_reason(exc)}")
            return 1
        try:
            written = record_phrases(phrases, folder, mic)
        finally:
            mic.close()
        print(f"Saved {written} recording(s) in {folder}")
        return 0

    providers = list(dict.fromkeys(n.strip() for n in args.providers.split(",") if n.strip()))
    unknown = [n for n in providers if n not in ("groq", "local")]
    if unknown:
        _err(f"bench_stt: unknown provider(s): {', '.join(unknown)} (use groq, local)")
        return 2
    cuda = probe_cuda() if args.cuda_probe else None

    folder = args.audio_dir / args.source
    if args.source == "recorded":
        clips = load_recorded_clips(phrases, args.audio_dir / "recorded", log=_err)
    else:
        try:
            clips = synthesize_clips(make_tts(args.source), phrases, folder, log=_err)
        except Exception as exc:  # TTS backend missing
            _err(f"bench_stt: cannot synthesize audio ({type(exc).__name__}: {_skip_reason(exc)})")
            return 1
    if not clips:
        _err("bench_stt: no audio clips to run (use --record, or check the TTS source)")
        return 1

    reports: list[VariantReport] = []
    for name in providers:
        if name == "groq":
            _err("Running groq ...")
            reports.append(_bench_groq(settings, clips, args))
        else:
            models = [m.strip() for m in (args.local_models or settings.stt.local.model).split(",")]
            for model in dict.fromkeys(m for m in models if m):
                _err(f"Running local/{model} ({args.local_device}) ...")
                reports.append(_bench_local(settings, model, clips, args))
    for r in reports:
        if r.skipped_reason:
            _err(f"Skipping {r.label}: {r.skipped_reason}")
    print(format_markdown(reports, cuda))
    if args.out:
        options = {
            "source": args.source,
            "runs": args.runs,
            "delay_s": args.delay,
            "local_device": args.local_device,
            "lang_hint": args.lang_hint,
            "clips": len(clips),
        }
        args.out.write_text(
            json.dumps(build_json(reports, options, cuda), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _err(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
