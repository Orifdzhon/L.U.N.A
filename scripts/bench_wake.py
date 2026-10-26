"""Wake-word benchmark: recall, false accepts per hour, latency and CPU of the Vosk spotter (#112).

Usage::

    uv run python scripts/bench_wake.py --synth                       # smoke run, SAPI voices
    uv run python scripts/bench_wake.py --clips D:\\wake-clips --negatives D:\\wake-long
                                        [--out wake.json] [--md wake.md]
    uv run python scripts/bench_wake.py --record D:\\wake-clips --tag 1m   # record clips

What it does: for every point of the grid ``mode x threshold x min_word_s x report_partials`` it
streams the audio frame by frame (30 ms, 16 kHz mono) through a fresh ``VoskKeywordSpotter`` and
applies the production cooldown (an accepted event is followed by ``detector.reset()`` and
``--cooldown`` seconds of ignored events). Metrics per configuration:

- recall: share of positive clips in which the keyword fired at least once;
- false accepts: events on negative clips plus events on the long negative recordings,
  FA/hour = false accepts / hours of negative audio;
- wake latency: audio time at which the event was produced minus the end of the keyword
  (``keyword_end_s`` of the manifest when given, else the end the engine itself reports), p50/p95;
  it excludes processing time, which is reported separately as
- CPU: process CPU seconds spent in ``process()`` per second of audio.

Targets (ADR 0004 amendment, D7): FA <= 0.5/h and recall >= 90 %. A configuration is marked
``yes`` only when both hold **and** at least ``--min-negative-hours`` of negative audio was
analysed (default 1 h); with less audio it is ``provisional``. Long recordings are expensive
(every grid point re-processes them), so they are analysed only for configurations that reach the
recall target on the clips (``--long-all`` disables that shortcut).

Inputs (all optional but one of ``--synth`` / ``--clips`` is needed):

- ``--clips DIR``: ``DIR/manifest.csv`` with columns ``file,label[,keyword_end_s[,note]]``
  (``label`` = positive|negative, ``file`` relative to the manifest), or, without a manifest,
  the folders ``DIR/positive/*.wav`` and ``DIR/negative/*.wav``. ``--record`` writes both.
  WAV only (16-bit; other rates and channels are converted; convert MP3 first, e.g. with ffmpeg).
- ``--negatives PATH``: a long WAV file or a folder of WAV files with speech that must not wake
  the assistant (room audio, podcasts, TV). Streamed from disk, so hours are fine.
- ``--synth``: phrases of ``benchmarks/wake_phrases.toml`` synthesized with Windows SAPI voices
  (cached under ``benchmarks/wake_audio/``, gitignored). The report is marked SYNTHETIC: TTS is
  clean, fluent and the same every time, so it only proves the pipeline works.

``--record DIR`` is a helper for the owner's calibration (#115): it prompts for each phrase of the
phrase file, records a fixed number of seconds from the configured microphone (the same capture
path as production) and files the clip as positive/negative. The folder must be outside the
repository: audio is never committed.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import math
import re
import sys
import time
import tomllib
import wave
from array import array
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Literal, Protocol

import bench_stt

from luna.config.loader import load_settings
from luna.core.errors import ConfigError
from luna.wakeword.base import WAKE_SAMPLE_RATE, WakeWordDetector
from luna.wakeword.verify import (
    VERIFY_AFTER_MS,
    VERIFY_LEAD_MS,
    WakeVerifier,
    keyword_first,
    verify_window,
)
from luna.wakeword.vosk_spotter import DEFAULT_MIN_WORD_S

SAMPLE_RATE = WAKE_SAMPLE_RATE
REPO = Path(__file__).resolve().parent.parent
DEFAULT_PHRASES = REPO / "benchmarks" / "wake_phrases.toml"
SYNTH_CACHE = REPO / "benchmarks" / "wake_audio" / "synth-sapi"

D7_MAX_FA_PER_HOUR = 0.5
D7_MIN_RECALL = 0.90
DEFAULT_COOLDOWN_S = 2.0  # [wakeword] cooldown_s default
DEFAULT_TAIL_S = 1.5  # silence fed after each clip: Vosk reports the keyword on the final result
DEFAULT_MIN_NEGATIVE_HOURS = 1.0
LONG_CHUNK_S = 10.0
MAX_FA_LISTED = 40
DEFAULT_VERIFY_TOP = 3
HISTORY_S = 10  # audio kept behind the stream head so a late event can still be cut

Label = Literal["positive", "negative"]
Verdict = Literal["yes", "provisional", "no"]


# -- grid --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    mode: str  # grammar | free
    threshold: float
    min_word_s: float
    report_partials: bool

    @property
    def label(self) -> str:
        partials = "partials" if self.report_partials else "finals"
        return f"{self.mode} thr={self.threshold:g} min={self.min_word_s:g} {partials}"


def build_grid(
    modes: Sequence[str],
    thresholds: Sequence[float],
    min_words: Sequence[float],
    partials: Sequence[bool],
) -> list[Config]:
    """Cartesian product ``mode x threshold x min_word_s x report_partials`` (stable order)."""
    for mode in modes:
        if mode not in ("grammar", "free"):
            raise ConfigError(f"unknown mode {mode!r}; use grammar or free")
    for t in thresholds:
        if not 0.0 <= t <= 1.0:
            raise ConfigError(f"threshold {t} is outside 0..1")
    for m in min_words:
        if m < 0:
            raise ConfigError(f"min_word_s {m} is negative")
    grid = [Config(*item) for item in product(modes, thresholds, min_words, partials)]
    if not grid:
        raise ConfigError("the grid is empty")
    return grid


def parse_floats(text: str) -> list[float]:
    try:
        values = [float(part) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise ConfigError(f"not a comma-separated list of numbers: {text!r}") from exc
    if not values:
        raise ConfigError(f"no numbers in {text!r}")
    return list(dict.fromkeys(values))


def parse_partials(text: str) -> list[bool]:
    table = {"on": [True], "off": [False], "both": [True, False]}
    if text not in table:
        raise ConfigError("--partials must be on, off or both")
    return table[text]


# -- audio input -------------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchClip:
    id: str
    label: Label
    pcm16: bytes
    keyword_end_s: float | None = None  # ground truth, when known
    category: str = ""

    @property
    def duration_s(self) -> float:
        return len(self.pcm16) / 2 / SAMPLE_RATE


def _clip_label(value: str, where: str) -> Label:
    label = value.strip().lower()
    if label == "positive":
        return "positive"
    if label == "negative":
        return "negative"
    raise ConfigError(f"{where}: label must be positive or negative, got {value!r}")


def load_clip_folder(
    folder: Path, *, log: Callable[[str], None] = lambda _m: None
) -> list[BenchClip]:
    """Clips from ``folder/manifest.csv`` or, without it, ``positive/`` and ``negative/``."""
    if not folder.is_dir():
        raise ConfigError(f"clips folder not found: {folder}")
    entries: list[tuple[Path, Label, float | None]] = []
    manifest = folder / "manifest.csv"
    if manifest.is_file():
        try:
            with manifest.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise ConfigError(f"cannot read {manifest}: {exc}") from exc
        for number, row in enumerate(rows, 2):
            where = f"{manifest.name} line {number}"
            name = (row.get("file") or "").strip()
            if not name or row.get("label") is None:
                raise ConfigError(f"{where}: columns 'file' and 'label' are required")
            path = (folder / name).resolve()
            if not path.is_relative_to(folder.resolve()):
                raise ConfigError(f"{where}: {name!r} points outside the clips folder")
            end_text = (row.get("keyword_end_s") or "").strip()
            try:
                end_s = float(end_text) if end_text else None
            except ValueError as exc:
                raise ConfigError(f"{where}: keyword_end_s is not a number") from exc
            entries.append((path, _clip_label(row["label"], where), end_s))
    else:
        for sub, label in (("positive", "positive"), ("negative", "negative")):
            for path in sorted((folder / sub).glob("*.wav")):
                entries.append((path, _clip_label(label, sub), None))
    clips: list[BenchClip] = []
    for path, label, end_s in entries:
        try:
            pcm = bench_stt.read_wav(path)
        except (wave.Error, EOFError, OSError, ValueError) as exc:
            log(f"cannot read {path.name}: {exc}; skipped")
            continue
        clips.append(BenchClip(path.stem, label, pcm, end_s))
    if not clips:
        raise ConfigError(f"no clips found in {folder}")
    return clips


def load_wake_phrases(path: Path) -> list[tuple[bench_stt.Phrase, Label]]:
    """Phrase file -> (phrase, label); ``category`` keeps the wake-phrase categories."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read phrases from {path}: {exc}") from exc
    result: list[tuple[bench_stt.Phrase, Label]] = []
    seen: set[str] = set()
    try:
        for raw in data.get("phrase", []):
            phrase = bench_stt.Phrase(raw["id"], raw["lang"], raw["category"], raw["text"])
            if phrase.id in seen:
                raise ConfigError(f"duplicate phrase id {phrase.id!r}")
            if phrase.lang not in ("ru", "en"):
                raise ConfigError(f"phrase {phrase.id!r}: lang must be ru or en")
            if not phrase.text.strip():
                raise ConfigError(f"phrase {phrase.id!r}: empty text")
            seen.add(phrase.id)
            result.append((phrase, _clip_label(raw["label"], f"phrase {phrase.id!r}")))
    except KeyError as exc:
        raise ConfigError(f"phrase entry without field {exc} in {path}") from exc
    if not result:
        raise ConfigError(f"no phrases in {path}")
    return result


def synth_clips(
    tts: bench_stt.TTSLike,
    phrases: Sequence[tuple[bench_stt.Phrase, Label]],
    folder: Path,
    *,
    skip_categories: Iterable[str] = (),
    log: Callable[[str], None] = lambda _m: None,
) -> list[BenchClip]:
    skipped = set(skip_categories)
    chosen = [(p, label) for p, label in phrases if p.category not in skipped]
    labels = {p.id: (label, p.category) for p, label in chosen}
    made = bench_stt.synthesize_clips(tts, [p for p, _ in chosen], folder, log=log)
    return [
        BenchClip(c.phrase.id, labels[c.phrase.id][0], c.pcm16, None, labels[c.phrase.id][1])
        for c in made
    ]


def iter_wav_chunks(path: Path, chunk_s: float = LONG_CHUNK_S) -> Iterator[bytes]:
    """Stream a 16-bit WAV as 16 kHz mono PCM chunks (any rate / channel count; first channel)."""
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        if width != 2:
            raise ValueError(f"{path.name}: expected 16-bit audio, got {width * 8}-bit")
        step = max(1, int(rate * chunk_s))
        while True:
            raw = wav.readframes(step)
            if not raw:
                return
            if channels > 1:
                samples = array("h")
                samples.frombytes(raw)
                if sys.byteorder == "big":
                    samples.byteswap()
                mono = samples[::channels]
                if sys.byteorder == "big":
                    mono.byteswap()
                raw = mono.tobytes()
            yield bench_stt.resample_pcm16(raw, rate, SAMPLE_RATE)


def find_long_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix.lower() == ".wav")
        if files:
            return files
        raise ConfigError(f"no .wav files in {path} (convert MP3 to WAV first)")
    raise ConfigError(f"negatives path not found: {path}")


def wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


# -- streaming and metrics ---------------------------------------------------------------------


@dataclass
class Detection:
    at_s: float  # audio time at which the event was produced
    keyword_end_s: float | None  # end of the keyword as reported by the engine
    keyword: str = ""
    window: bytes | None = None  # production verify_window at the cut (only with keep_windows)

    @property
    def cut_s(self) -> float:
        """The wake cut: end of the keyword when the engine reports it, else the event time."""
        return self.at_s if self.keyword_end_s is None else self.keyword_end_s


class StreamRunner:
    """Feeds PCM to a detector frame by frame with the production cooldown.

    An accepted event is followed by ``detector.reset()`` (like the listener does) and events
    during the next ``cooldown_s`` seconds are dropped. Tracks audio seconds and the CPU time
    spent inside ``process()``.
    """

    def __init__(
        self,
        detector: WakeWordDetector,
        *,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        clock: Callable[[], float] = time.process_time,
        keep_windows: bool = False,
    ) -> None:
        self._det = detector
        self._frame_bytes = detector.frame_samples * 2
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._pending = b""
        self._samples = 0  # samples fed so far
        self._origin = 0  # sample index of the last reset()
        self._last_accept_s: float | None = None
        self.cpu_s = 0.0
        self.detections: list[Detection] = []
        # window capture for stage 2: a bounded buffer of recent audio plus detections that
        # still wait for their "after the cut" audio
        self._keep = keep_windows
        self._buf = bytearray()
        self._buf_origin = 0  # sample index of _buf[0]
        self._waiting: list[Detection] = []

    @property
    def audio_s(self) -> float:
        return self._samples / SAMPLE_RATE

    def feed(self, pcm: bytes) -> None:
        data = self._pending + pcm
        size = self._frame_bytes
        usable = len(data) - len(data) % size
        for start in range(0, usable, size):
            self._process(data[start : start + size])
        self._pending = data[usable:]

    def flush(self, tail_s: float = 0.0) -> None:
        """Pad the partial frame with silence and feed ``tail_s`` of silence."""
        if self._pending:
            self.feed(bytes(self._frame_bytes - len(self._pending)))
        if tail_s > 0:
            self.feed(
                bytes(int(tail_s * SAMPLE_RATE) // self._det.frame_samples * self._frame_bytes)
            )
        if self._keep:
            self._settle(final=True)

    def _process(self, frame: bytes) -> None:
        began = self._clock()
        event = self._det.process(frame)
        self.cpu_s += self._clock() - began
        self._samples += self._det.frame_samples
        if self._keep:
            self._buf += frame
        try:
            self._handle(event)
        finally:
            if self._keep:
                self._settle(final=False)

    def _handle(self, event: object) -> None:
        if event is None:
            return
        at_s = self._samples / SAMPLE_RATE
        last = self._last_accept_s
        if last is not None and at_s - last < self._cooldown_s:
            return
        self._last_accept_s = at_s
        end_s = (
            None if event.end_sample is None else (self._origin + event.end_sample) / SAMPLE_RATE
        )
        detection = Detection(at_s, end_s, event.keyword)  # type: ignore[attr-defined]
        self.detections.append(detection)
        if self._keep:
            self._waiting.append(detection)
        self._det.reset()
        self._origin = self._samples

    def _settle(self, *, final: bool) -> None:
        """Cut windows whose "after" audio has arrived (all of them at the end of the audio),
        then drop audio nobody can still need."""
        after = VERIFY_AFTER_MS * SAMPLE_RATE // 1000
        lead = VERIFY_LEAD_MS * SAMPLE_RATE // 1000
        still: list[Detection] = []
        for det in self._waiting:
            cut = round(det.cut_s * SAMPLE_RATE)
            if final or self._samples >= cut + after:
                rel = min(max(0, cut - self._buf_origin), len(self._buf) // 2) * 2
                det.window = verify_window(bytes(self._buf[:rel]), [bytes(self._buf[rel:])])
            else:
                still.append(det)
        self._waiting = still
        # The engine reports an event up to a few seconds after the keyword ended (finals), so
        # keep a generous history; production has the same audio in the listener's ring buffer.
        horizon = min([round(d.cut_s * SAMPLE_RATE) for d in still] + [self._samples])
        keep_from = min(horizon - lead, self._samples - HISTORY_S * SAMPLE_RATE)
        drop = min(max(0, keep_from - self._buf_origin), len(self._buf) // 2)
        if drop:
            del self._buf[: drop * 2]
            self._buf_origin += drop


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100); ``None`` for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def wake_latency_s(clip: BenchClip, detection: Detection) -> float | None:
    """Audio time from the end of the keyword to the event."""
    end = clip.keyword_end_s if clip.keyword_end_s is not None else detection.keyword_end_s
    return None if end is None else detection.at_s - end


@dataclass
class FalseAccept:
    source: str
    at_s: float


@dataclass
class Stage2:
    """Metrics after the verification stage (``--verify local``) for one configuration."""

    hits: int = 0  # positive clips with at least one accepted detection
    strict_hits: int = 0  # ... accepted under "keyword followed by punctuation or end"
    false_rejects: list[str] = field(default_factory=list)  # hit by stage 1, rejected by stage 2
    fa_clips: int = 0  # accepted detections on negative clips
    fa_long: int = 0
    strict_fa: int = 0  # accepted under the strict rule (clips + long)
    negative_rejects: int = 0  # stage-1 false accepts the verifier threw out
    errors: int = 0  # decode errors (counted as rejects: the bench runs fail-closed)
    checks: int = 0
    wall_s: list[float] = field(default_factory=list)
    cpu_s: list[float] = field(default_factory=list)

    @property
    def fa_total(self) -> int:
        return self.fa_clips + self.fa_long


@dataclass
class ConfigResult:
    config: Config
    positives: int = 0
    hits: int = 0
    misses: list[str] = field(default_factory=list)
    fa_clips: int = 0
    fa_long: int = 0
    clip_negative_s: float = 0.0
    long_negative_s: float = 0.0
    long_skipped: bool = False
    latencies_s: list[float] = field(default_factory=list)
    audio_s: float = 0.0
    cpu_s: float = 0.0
    false_accepts: list[FalseAccept] = field(default_factory=list)
    # stage 2: windows kept for the verifier, and its outcome (None = not verified)
    pos_windows: dict[str, list[bytes]] = field(default_factory=dict)
    neg_windows: list[tuple[str, float, bytes, bool]] = field(default_factory=list)  # ..., long
    stage2: Stage2 | None = None

    @property
    def recall(self) -> float | None:
        return self.hits / self.positives if self.positives else None

    @property
    def negative_hours(self) -> float:
        return (self.clip_negative_s + self.long_negative_s) / 3600

    @property
    def false_accepts_total(self) -> int:
        return self.fa_clips + self.fa_long

    @property
    def fa_per_hour(self) -> float | None:
        hours = self.negative_hours
        return self.false_accepts_total / hours if hours > 0 else None

    @property
    def cpu_per_audio_s(self) -> float | None:
        return self.cpu_s / self.audio_s if self.audio_s > 0 else None

    @property
    def recall2(self) -> float | None:
        s2 = self.stage2
        return s2.hits / self.positives if s2 is not None and self.positives else None

    @property
    def fa_per_hour2(self) -> float | None:
        s2 = self.stage2
        hours = self.negative_hours
        return s2.fa_total / hours if s2 is not None and hours > 0 else None

    def verdict2(
        self,
        *,
        min_negative_hours: float = DEFAULT_MIN_NEGATIVE_HOURS,
        max_fa_per_hour: float = D7_MAX_FA_PER_HOUR,
        min_recall: float = D7_MIN_RECALL,
    ) -> Verdict:
        """The D7 verdict on the two-stage pipeline (stage-2 recall and FA/h)."""
        recall, fa = self.recall2, self.fa_per_hour2
        if recall is None or fa is None or self.long_skipped:
            return "no"
        if recall < min_recall or fa > max_fa_per_hour:
            return "no"
        return "yes" if self.negative_hours >= min_negative_hours else "provisional"

    def verdict(
        self,
        *,
        min_negative_hours: float = DEFAULT_MIN_NEGATIVE_HOURS,
        max_fa_per_hour: float = D7_MAX_FA_PER_HOUR,
        min_recall: float = D7_MIN_RECALL,
    ) -> Verdict:
        """``yes`` meets D7 on enough negative audio, ``provisional`` meets it on too little."""
        recall, fa = self.recall, self.fa_per_hour
        if recall is None or fa is None or self.long_skipped:
            return "no"
        if recall < min_recall or fa > max_fa_per_hour:
            return "no"
        return "yes" if self.negative_hours >= min_negative_hours else "provisional"


DetectorFactory = Callable[[Config], WakeWordDetector]


def _windows(detections: Sequence[Detection]) -> list[bytes]:
    return [d.window for d in detections if d.window is not None]


def evaluate_config(
    config: Config,
    factory: DetectorFactory,
    clips: Sequence[BenchClip],
    long_files: Sequence[Path] = (),
    *,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    tail_s: float = DEFAULT_TAIL_S,
    long_gate: Callable[[ConfigResult], bool] = lambda _r: True,
    clock: Callable[[], float] = time.process_time,
    long_reader: Callable[[Path], Iterator[bytes]] = iter_wav_chunks,
    long_duration: Callable[[Path], float] = wav_duration_s,
    keep_windows: bool = False,
) -> ConfigResult:
    """Run one configuration over the clips and (when ``run_long``) the long recordings."""
    result = ConfigResult(config)
    detector = factory(config)
    try:
        for clip in clips:
            detector.reset()
            runner = StreamRunner(
                detector, cooldown_s=cooldown_s, clock=clock, keep_windows=keep_windows
            )
            runner.feed(clip.pcm16)
            runner.flush(tail_s)
            result.audio_s += runner.audio_s
            result.cpu_s += runner.cpu_s
            if clip.label == "positive":
                result.positives += 1
                if runner.detections:
                    result.hits += 1
                    result.pos_windows[clip.id] = _windows(runner.detections)
                    latency = wake_latency_s(clip, runner.detections[0])
                    if latency is not None:
                        result.latencies_s.append(latency)
                else:
                    result.misses.append(clip.id)
            else:
                result.clip_negative_s += clip.duration_s
                result.fa_clips += len(runner.detections)
                result.false_accepts.extend(FalseAccept(clip.id, d.at_s) for d in runner.detections)
                result.neg_windows.extend(
                    (clip.id, d.at_s, d.window, False)
                    for d in runner.detections
                    if d.window is not None
                )
        if long_files and not long_gate(result):
            result.long_skipped = True
        elif long_files:
            for path in long_files:
                detector.reset()
                runner = StreamRunner(
                    detector, cooldown_s=cooldown_s, clock=clock, keep_windows=keep_windows
                )
                for chunk in long_reader(path):
                    runner.feed(chunk)
                runner.flush(tail_s)
                result.audio_s += runner.audio_s
                result.cpu_s += runner.cpu_s
                result.long_negative_s += max(0.0, runner.audio_s - tail_s)
                result.fa_long += len(runner.detections)
                result.false_accepts.extend(
                    FalseAccept(path.name, d.at_s) for d in runner.detections
                )
                result.neg_windows.extend(
                    (path.name, d.at_s, d.window, True)
                    for d in runner.detections
                    if d.window is not None
                )
    finally:
        close = getattr(detector, "close", None)
        if close is not None:
            close()
    return result


def run_grid(
    grid: Sequence[Config],
    factory: DetectorFactory,
    clips: Sequence[BenchClip],
    long_files: Sequence[Path] = (),
    *,
    long_all: bool = False,
    min_recall: float = D7_MIN_RECALL,
    progress: Callable[[str], None] = lambda _m: None,
    **kwargs: object,
) -> list[ConfigResult]:
    """Evaluate every configuration. The (slow) long recordings are only analysed for
    configurations whose recall on the clips reaches ``min_recall`` unless ``long_all``."""

    def gate(result: ConfigResult) -> bool:
        return long_all or (result.recall is not None and result.recall >= min_recall)

    results: list[ConfigResult] = []
    for index, config in enumerate(grid, 1):
        progress(f"[{index}/{len(grid)}] {config.label}")
        results.append(
            evaluate_config(
                config,
                factory,
                clips,
                long_files,
                long_gate=gate,
                **kwargs,  # type: ignore[arg-type]
            )
        )
    return results


# -- stage 2: verification ---------------------------------------------------------------------

_LETTERS = re.compile(r"\W*[^\W\d_]+(?:-[^\W\d_]+)*(?P<rest>.*)", re.DOTALL)


def keyword_strict(text: str, words: Sequence[str]) -> bool:
    """Stricter rule (bench only): the production first-token match, and the keyword is then
    followed by punctuation or the end of the text ("Луна, открой" yes, "Луна открой" no)."""
    if not keyword_first(text, words):
        return False
    match = _LETTERS.match(text)
    if match is None:
        return False
    rest = match.group("rest").lstrip()
    return not rest or not (rest[0].isalnum() or rest[0] == "_")


def select_for_verification(results: Sequence[ConfigResult], top: int) -> list[ConfigResult]:
    """The ``top`` configurations by stage-1 recall (ties: lower FA/h, then grid order)."""
    ordered = sorted(
        results,
        key=lambda r: (
            -(r.recall if r.recall is not None else -1.0),
            r.fa_per_hour if r.fa_per_hour is not None else math.inf,
        ),
    )
    return ordered[: max(0, top)]


def verify_result(
    result: ConfigResult,
    verifier: WakeVerifier,
    words: Sequence[str],
    *,
    cpu_clock: Callable[[], float] = time.process_time,
) -> Stage2:
    """Run the verifier over the windows stage 1 produced for this configuration."""
    s2 = Stage2()

    def check(window: bytes) -> tuple[bool, bool]:
        began = cpu_clock()
        outcome = verifier.verify(window)
        s2.cpu_s.append(cpu_clock() - began)
        s2.wall_s.append(outcome.latency_s)
        s2.checks += 1
        if outcome.reason == "error":
            s2.errors += 1
            return False, False
        return outcome.accepted, outcome.accepted and keyword_strict(outcome.text, words)

    for clip_id, windows in result.pos_windows.items():
        loose = strict = False
        for window in windows:
            ok, strict_ok = check(window)
            loose, strict = loose or ok, strict or strict_ok
            if loose and strict:
                break
        s2.hits += loose
        s2.strict_hits += strict
        if not loose:
            s2.false_rejects.append(clip_id)
    for _source, _at_s, window, is_long in result.neg_windows:
        ok, strict_ok = check(window)
        if not ok:
            s2.negative_rejects += 1
            continue
        s2.strict_fa += strict_ok
        if is_long:
            s2.fa_long += 1
        else:
            s2.fa_clips += 1
    return s2


# -- report ------------------------------------------------------------------------------------


def _fmt(value: float | None, spec: str = ".2f", missing: str = "-") -> str:
    return missing if value is None else format(value, spec)


def sort_results(results: Sequence[ConfigResult], **verdict_kwargs: float) -> list[ConfigResult]:
    rank = {"yes": 0, "provisional": 1, "no": 2}
    return sorted(
        results,
        key=lambda r: (
            rank[r.verdict(**verdict_kwargs)],
            -(r.recall if r.recall is not None else -1.0),
            r.fa_per_hour if r.fa_per_hour is not None else math.inf,
        ),
    )


def markdown_report(
    results: Sequence[ConfigResult],
    *,
    synthetic: bool,
    min_negative_hours: float = DEFAULT_MIN_NEGATIVE_HOURS,
    header: str = "",
) -> str:
    lines = ["# Wake-word benchmark", ""]
    if synthetic:
        lines += [
            "> **SYNTHETIC**: the clips were produced by a TTS voice. Clean, fluent and identical "
            "on "
            "every run, so these numbers say nothing about real speech, a quiet microphone or room "
            "noise. They only prove that the pipeline runs. Do not use them for D7.",
            "",
        ]
    if header:
        lines += [header, ""]
    lines += [
        f"Targets (ADR 0004, D7): FA <= {D7_MAX_FA_PER_HOUR}/h and recall >= "
        f"{D7_MIN_RECALL:.0%}. `yes` = both met on >= {min_negative_hours:g} h of negative audio; "
        "`provisional` = met on less audio (FA/h is not meaningful yet); `no` = not met or the "
        "long "
        "recordings were skipped because recall on the clips was already too low.",
        "",
        "| D7 | mode | thr | min_word_s | partials | recall % | hits | FA clips | FA long | "
        "neg h | FA/h | lat p50 s | lat p95 s | CPU s/s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    verdict_kw = {"min_negative_hours": min_negative_hours}
    for r in sort_results(results, **verdict_kw):
        verdict = r.verdict(**verdict_kw)
        mark = {"yes": "**yes**", "provisional": "provisional", "no": "no"}[verdict]
        recall = None if r.recall is None else r.recall * 100
        fa_long = "skipped" if r.long_skipped else str(r.fa_long)
        row = [
            mark,
            r.config.mode,
            f"{r.config.threshold:g}",
            f"{r.config.min_word_s:g}",
            "on" if r.config.report_partials else "off",
            _fmt(recall, ".1f"),
            f"{r.hits}/{r.positives}",
            str(r.fa_clips),
            fa_long,
            _fmt(r.negative_hours, ".3f"),
            _fmt(r.fa_per_hour, ".2f"),
            _fmt(percentile(r.latencies_s, 50)),
            _fmt(percentile(r.latencies_s, 95)),
            _fmt(r.cpu_per_audio_s, ".3f"),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines += stage2_markdown(results, min_negative_hours)
    return "\n".join(lines)


def stage2_markdown(results: Sequence[ConfigResult], min_negative_hours: float) -> list[str]:
    """The two-stage table; empty (output unchanged) when nothing was verified."""
    verified = [r for r in results if r.stage2 is not None]
    if not verified:
        return []
    kw = {"min_negative_hours": min_negative_hours}
    rank = {"yes": 0, "provisional": 1, "no": 2}
    verified.sort(
        key=lambda r: (
            rank[r.verdict2(**kw)],
            -(r.recall2 if r.recall2 is not None else -1.0),
            r.fa_per_hour2 if r.fa_per_hour2 is not None else math.inf,
        )
    )
    lines = [
        f"## Stage 2: Vosk + local verification (top {len(verified)} of {len(results)} "
        "configs by stage-1 recall)",
        "",
        "Recall and FA are counted **after** the verifier. `false rej` = positive clips the "
        "first stage caught and the verifier threw out; `neg rej` = stage-1 false accepts the "
        "verifier removed. `strict` = the stricter rule (keyword followed by punctuation or the "
        "end of the transcript). Decode times are wall clock; "
        "CPU is process CPU seconds per check.",
        "",
        "| D7 | mode | thr | min_word_s | partials | recall1 % | recall2 % | false rej | "
        "FA1 | FA2 clips | FA2 long | neg h | FA2/h | neg rej | wall p50 s | wall p95 s | "
        "CPU s/check | strict recall % | strict FA |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in verified:
        s2 = r.stage2
        assert s2 is not None
        mark = {"yes": "**yes**", "provisional": "provisional", "no": "no"}[r.verdict2(**kw)]
        recall1 = None if r.recall is None else r.recall * 100
        recall2 = None if r.recall2 is None else r.recall2 * 100
        strict = s2.strict_hits / r.positives * 100 if r.positives else None
        row = [
            mark,
            r.config.mode,
            f"{r.config.threshold:g}",
            f"{r.config.min_word_s:g}",
            "on" if r.config.report_partials else "off",
            _fmt(recall1, ".1f"),
            _fmt(recall2, ".1f"),
            f"{len(s2.false_rejects)}/{r.hits}",
            str(r.false_accepts_total),
            str(s2.fa_clips),
            "skipped" if r.long_skipped else str(s2.fa_long),
            _fmt(r.negative_hours, ".3f"),
            _fmt(r.fa_per_hour2, ".2f"),
            str(s2.negative_rejects),
            _fmt(percentile(s2.wall_s, 50)),
            _fmt(percentile(s2.wall_s, 95)),
            _fmt(sum(s2.cpu_s) / s2.checks if s2.checks else None, ".2f"),
            _fmt(strict, ".1f"),
            str(s2.strict_fa),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    for r in verified:
        s2 = r.stage2
        assert s2 is not None
        note = f"- {r.config.label}: false rejects: " + (
            ", ".join(s2.false_rejects) if s2.false_rejects else "none"
        )
        if s2.errors:
            note += f"; {s2.errors} decode error(s) counted as rejects"
        lines.append(note)
    lines.append("")
    return lines


def json_report(
    results: Sequence[ConfigResult],
    *,
    synthetic: bool,
    options: dict[str, object],
    min_negative_hours: float = DEFAULT_MIN_NEGATIVE_HOURS,
) -> dict[str, object]:
    rows = []
    for r in sort_results(results, min_negative_hours=min_negative_hours):
        rows.append(
            {
                "config": asdict(r.config),
                "verdict": r.verdict(min_negative_hours=min_negative_hours),
                "positives": r.positives,
                "hits": r.hits,
                "recall": r.recall,
                "missed_clips": r.misses,
                "fa_clips": r.fa_clips,
                "fa_long": r.fa_long,
                "long_skipped": r.long_skipped,
                "negative_hours": r.negative_hours,
                "fa_per_hour": r.fa_per_hour,
                "latency_p50_s": percentile(r.latencies_s, 50),
                "latency_p95_s": percentile(r.latencies_s, 95),
                "cpu_s_per_audio_s": r.cpu_per_audio_s,
                "false_accept_events": [
                    {"source": fa.source, "at_s": round(fa.at_s, 2)}
                    for fa in r.false_accepts[:MAX_FA_LISTED]
                ],
            }
        )
        if r.stage2 is not None:
            s2 = r.stage2
            rows[-1]["stage2"] = {
                "hits": s2.hits,
                "recall": r.recall2,
                "strict_hits": s2.strict_hits,
                "false_rejects": s2.false_rejects,
                "fa_clips": s2.fa_clips,
                "fa_long": s2.fa_long,
                "fa_per_hour": r.fa_per_hour2,
                "strict_fa": s2.strict_fa,
                "negative_rejects": s2.negative_rejects,
                "errors": s2.errors,
                "checks": s2.checks,
                "verify_wall_p50_s": percentile(s2.wall_s, 50),
                "verify_wall_p95_s": percentile(s2.wall_s, 95),
                "verify_cpu_s_per_check": sum(s2.cpu_s) / s2.checks if s2.checks else None,
                "verdict": r.verdict2(min_negative_hours=min_negative_hours),
            }
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "synthetic": synthetic,
        "targets": {"max_fa_per_hour": D7_MAX_FA_PER_HOUR, "min_recall": D7_MIN_RECALL},
        "min_negative_hours": min_negative_hours,
        "options": options,
        "results": rows,
    }


# -- recording helper --------------------------------------------------------------------------


def ensure_outside_repo(path: Path, repo: Path = REPO) -> Path:
    """Recorded audio must never land in the repository."""
    resolved = path.expanduser().resolve()
    if resolved == repo.resolve() or resolved.is_relative_to(repo.resolve()):
        raise ConfigError(f"{path} is inside the repository; choose a folder outside it")
    return resolved


class ClipRecorder(Protocol):
    def capture(self, seconds: float) -> bytes:
        """Record ``seconds`` of 16 kHz mono PCM16 from the microphone."""
        ...


def rms_of(pcm16: bytes) -> float:
    """RMS level in 0..1 of full scale."""
    samples = array("h")
    samples.frombytes(pcm16[: len(pcm16) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768


def _append_manifest(folder: Path, rows: list[tuple[str, Label, str]]) -> None:
    manifest = folder / "manifest.csv"
    new = not manifest.exists()
    with manifest.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["file", "label", "keyword_end_s", "note"])
        for name, label, note in rows:
            writer.writerow([name, label, "", note])


def record_session(
    phrases: Sequence[tuple[bench_stt.Phrase, Label]],
    folder: Path,
    recorder: ClipRecorder,
    *,
    seconds: float = 4.0,
    tag: str = "",
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    """Prompt for each phrase, record ``seconds`` and file the clip under positive/ or negative/.

    Existing files are never overwritten. ``q`` quits, ``s`` skips. Returns the clips written.
    """
    written = 0
    suffix = f"-{tag}" if tag else ""
    for index, (phrase, label) in enumerate(phrases, 1):
        rel = f"{label}/{phrase.id}{suffix}.wav"
        path = folder / rel
        if path.exists():
            out(f"[{index}/{len(phrases)}] {rel} exists, not overwriting")
            continue
        while True:
            hint = "say it" if label == "positive" else "say it (must NOT wake the assistant)"
            answer = (
                input_fn(
                    f"[{index}/{len(phrases)}] {label.upper()}: «{phrase.text}»  {hint}.  "
                    f"Enter = record {seconds:g} s, s = skip, q = quit "
                )
                .strip()
                .lower()
            )
            if answer == "q":
                return written
            if answer == "s":
                break
            pcm = recorder.capture(seconds)
            level = rms_of(pcm)
            out(f"  recorded {len(pcm) / 2 / SAMPLE_RATE:.1f} s, level (RMS) {level:.4f}")
            if level < 0.0005:
                out("  almost silent: is the microphone muted or the wrong device selected?")
            if input_fn("  Enter = keep, r = redo ").strip().lower() == "r":
                continue
            bench_stt.write_wav(path, pcm)
            _append_manifest(folder, [(rel, label, f"{phrase.category}; {phrase.text}")])
            written += 1
            break
    return written


class MicClipRecorder:
    """Fixed-length capture from the configured microphone (production capture path)."""

    def __init__(self, device: int | str | None, frame_ms: int = 30) -> None:
        from luna.audio.capture import SoundDeviceMic

        self._frame_ms = frame_ms
        self._mic = SoundDeviceMic(device, frame_ms, 0)

    def capture(self, seconds: float) -> bytes:
        frames: list[bytes] = []
        wanted = max(1, round(seconds * 1000 / self._frame_ms))
        self._mic.start()
        try:
            deadline = time.monotonic() + seconds + 2.0
            while len(frames) < wanted and time.monotonic() < deadline:
                frame = self._mic.read(0.5)
                if frame:
                    frames.append(frame)
        finally:
            self._mic.stop()
        return b"".join(frames)

    def close(self) -> None:
        self._mic.close()


# -- CLI ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Wake-word benchmark: recall, false accepts per hour, latency, CPU."
    )
    src = p.add_argument_group("audio")
    src.add_argument("--synth", action="store_true", help="synthesize clips with SAPI (SYNTHETIC)")
    src.add_argument("--clips", type=Path, help="folder with manifest.csv or positive/ negative/")
    src.add_argument("--negatives", type=Path, help="long WAV file or folder of WAV files")
    src.add_argument("--phrases", type=Path, default=DEFAULT_PHRASES)
    src.add_argument(
        "--skip-categories",
        default="en",
        help="phrase categories left out of --synth / --record (default: en, the model is Russian)",
    )
    rec = p.add_argument_group("recording")
    rec.add_argument(
        "--record", type=Path, metavar="DIR", help="record clips into DIR (outside repo)"
    )
    rec.add_argument("--tag", default="", help="file name suffix for --record, e.g. 1m or 3m")
    rec.add_argument("--record-seconds", type=float, default=4.0)
    grid = p.add_argument_group("grid")
    grid.add_argument("--modes", default="grammar,free")
    grid.add_argument("--thresholds", default="0.3,0.5,0.7")
    grid.add_argument("--min-word", default=f"0,{DEFAULT_MIN_WORD_S:g}")
    grid.add_argument("--partials", default="both", help="on | off | both")
    run = p.add_argument_group("run")
    run.add_argument("--keyword", default="луна")
    run.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_S)
    run.add_argument(
        "--tail", type=float, default=DEFAULT_TAIL_S, help="silence after each clip, s"
    )
    run.add_argument("--min-negative-hours", type=float, default=DEFAULT_MIN_NEGATIVE_HOURS)
    run.add_argument("--long-all", action="store_true", help="analyse long files for every config")
    run.add_argument(
        "--model-dir", type=Path, help="folder holding the Vosk model (default: shared)"
    )
    ver = p.add_argument_group("verification (stage 2)")
    ver.add_argument(
        "--verify",
        choices=("off", "local"),
        default="off",
        help="local = also run the production faster-whisper verifier on every stage-1 event",
    )
    ver.add_argument(
        "--verify-top",
        type=int,
        default=DEFAULT_VERIFY_TOP,
        help=f"verify only the N best configs by stage-1 recall (default {DEFAULT_VERIFY_TOP})",
    )
    out = p.add_argument_group("output")
    out.add_argument("--out", type=Path, help="write the results as JSON")
    out.add_argument("--md", type=Path, help="write the markdown table to a file")
    p.add_argument("--config", type=Path, help="path to luna.toml")
    return p


def make_vosk_factory(model_dir: Path | None, keyword: str) -> DetectorFactory:
    """Detector factory for the real Vosk spotter (downloads the model on first use)."""
    from luna.voice.wake_model import WakeModelStore
    from luna.wakeword.vosk_spotter import VoskKeywordSpotter

    model_path = WakeModelStore(model_dir).ensure()

    def factory(config: Config) -> WakeWordDetector:
        return VoskKeywordSpotter(
            model_path,
            [keyword],
            threshold=config.threshold,
            mode=config.mode,  # type: ignore[arg-type]
            min_word_s=config.min_word_s,
            report_partials=config.report_partials,
        )

    return factory


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def make_local_verifier(config_path: Path | None) -> tuple[WakeVerifier, list[str]]:
    """Production verifier on the ``[stt.local]`` model, loaded once (fail-closed in the bench)."""
    from luna.stt.faster_whisper import FasterWhisperSTT
    from luna.wakeword.verify import FasterWhisperVerifier

    settings = load_settings(config_path)
    conf = settings.stt.local
    words = list(settings.wakeword.verify_words)
    stt = FasterWhisperSTT(
        conf.model, conf.device, conf.compute_type, conf.beam_size, conf.model_dir
    )
    verifier = FasterWhisperVerifier(stt, words, fail_open=False)
    verifier.warm_up()
    return verifier, words


def main(
    argv: Sequence[str] | None = None,
    *,
    factory: DetectorFactory | None = None,
    verifier: tuple[WakeVerifier, list[str]] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    skip = [c.strip() for c in args.skip_categories.split(",") if c.strip()]
    try:
        from luna.ui.cli import configure_console_encoding

        configure_console_encoding()
        if args.record:
            return _record(args, skip)
        if not args.synth and not args.clips:
            raise ConfigError("give --synth, --clips DIR or --record DIR (see --help)")
        if args.cooldown < 0 or args.tail < 0 or args.min_negative_hours < 0:
            raise ConfigError("--cooldown, --tail and --min-negative-hours must be >= 0")
        if args.verify_top < 1:
            raise ConfigError("--verify-top must be >= 1")
        grid = build_grid(
            [m.strip() for m in args.modes.split(",") if m.strip()],
            parse_floats(args.thresholds),
            parse_floats(args.min_word),
            parse_partials(args.partials),
        )
        clips: list[BenchClip] = []
        if args.clips:
            clips += load_clip_folder(args.clips, log=_err)
        if args.synth:
            from luna.tts.sapi import SapiTTS

            phrases = load_wake_phrases(args.phrases)
            clips += synth_clips(SapiTTS(), phrases, SYNTH_CACHE, skip_categories=skip, log=_err)
        long_files = find_long_files(args.negatives) if args.negatives else []
        if not any(c.label == "positive" for c in clips):
            raise ConfigError("no positive clips: recall cannot be measured")
        detector_factory = factory or make_vosk_factory(args.model_dir, args.keyword)
        stage2: tuple[WakeVerifier, list[str]] | None = None
        if args.verify == "local":
            stage2 = verifier or make_local_verifier(args.config)
    except (OSError, ConfigError) as exc:
        _err(f"bench_wake: {exc}")
        return 2
    except Exception as exc:  # a provider error (model download, vosk) is reported, not a traceback
        from luna.core.errors import ProviderError

        if isinstance(exc, ProviderError):
            _err(f"bench_wake: {exc}")
            return 2
        raise
    logging.getLogger("luna").setLevel(logging.ERROR)
    long_hours = sum(wav_duration_s(p) for p in long_files) / 3600
    pos = sum(c.label == "positive" for c in clips)
    header = (
        f"Clips: {pos} positive, {len(clips) - pos} negative; long negative recordings: "
        f"{len(long_files)} file(s), {long_hours:.2f} h."
    )
    _err(header)
    results = run_grid(
        grid,
        detector_factory,
        clips,
        long_files,
        long_all=args.long_all,
        progress=_err,
        cooldown_s=args.cooldown,
        tail_s=args.tail,
        keep_windows=stage2 is not None,
    )
    if stage2 is not None:
        stage2_verifier, words = stage2
        try:
            for r in select_for_verification(results, args.verify_top):
                _err(f"verifying {r.config.label}")
                r.stage2 = verify_result(r, stage2_verifier, words)
        finally:
            stage2_verifier.close()
    synthetic = bool(args.synth)
    print(
        markdown_report(
            results,
            synthetic=synthetic,
            min_negative_hours=args.min_negative_hours,
            header=header,
        )
    )
    if args.md:
        args.md.write_text(
            markdown_report(
                results,
                synthetic=synthetic,
                min_negative_hours=args.min_negative_hours,
                header=header,
            ),
            encoding="utf-8",
        )
    if args.out:
        options = {
            "keyword": args.keyword,
            "cooldown_s": args.cooldown,
            "tail_s": args.tail,
            "clips": len(clips),
            "long_files": [p.name for p in long_files],
            "long_hours": long_hours,
            "long_all": args.long_all,
        }
        if stage2 is not None:
            options["verify"] = args.verify
            options["verify_top"] = args.verify_top
        args.out.write_text(
            json.dumps(
                json_report(
                    results,
                    synthetic=synthetic,
                    options=options,
                    min_negative_hours=args.min_negative_hours,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _err(f"Wrote {args.out}")
    return 0


def _record(args: argparse.Namespace, skip: list[str]) -> int:
    try:
        folder = ensure_outside_repo(args.record)
        phrases = [(p, lbl) for p, lbl in load_wake_phrases(args.phrases) if p.category not in skip]
        settings = load_settings(args.config)
        if args.record_seconds <= 0:
            raise ConfigError("--record-seconds must be > 0")
    except (OSError, ConfigError) as exc:
        _err(f"bench_wake: {exc}")
        return 2
    folder.mkdir(parents=True, exist_ok=True)
    recorder = MicClipRecorder(settings.voice.input_device, settings.voice.frame_ms)
    try:
        written = record_session(
            phrases, folder, recorder, seconds=args.record_seconds, tag=args.tag
        )
    finally:
        recorder.close()
    print(f"Wrote {written} clip(s) to {folder}")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
