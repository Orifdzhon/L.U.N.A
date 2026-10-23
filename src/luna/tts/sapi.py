"""``SapiTTS``: speech from the Windows built-in SAPI voices (ADR 0005 and its amendment).

Integration: Windows PowerShell + ``System.Speech`` in a short-lived **subprocess** (no extra
dependency, killable on timeout, no COM threading in our process). The audio comes back as a
WAV stream on stdout and is converted to an :class:`AudioClip`, so the interruptible player
plays it.

Security: the text to speak is untrusted. The PowerShell scripts below are **constants**; the
text, voice name, rate and volume travel as one ASCII-only JSON document on **stdin** and are
only ever used as data (``ConvertFrom-Json`` -> ``Speak(string)``, plain text: no SSML, no
evaluation). Nothing is interpolated into a command line or script, no ``shell=True``.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from luna.audio.base import SAMPLE_WIDTH_BYTES, AudioClip
from luna.core.errors import ProviderRequestError, ProviderUnavailable

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000  # hard cap (ConvertFrom-Json in PS 5.1 is limited to 2 MB)
_STDERR_TAIL = 300
MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # cap on what we accept back from the child process
VOICE_RETRY_S = 300.0  # after a failed voice query use the default voice for this long

_PREAMBLE = (
    "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
)

_LIST_SCRIPT = (
    _PREAMBLE + "$v = @($s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { "
    "@{ name = $_.VoiceInfo.Name; culture = $_.VoiceInfo.Culture.Name } }); "
    "$j = [System.Text.UTF8Encoding]::new($false)"
    ".GetBytes((ConvertTo-Json -InputObject $v -Compress)); "
    "$o = [Console]::OpenStandardOutput(); $o.Write($j, 0, $j.Length); $o.Flush()"
)

_SPEAK_SCRIPT = (
    _PREAMBLE + "$req = [Console]::In.ReadToEnd() | ConvertFrom-Json; "
    "if ($req.voice) { $s.SelectVoice([string]$req.voice) }; "
    "$s.Rate = [int]$req.rate; $s.Volume = [int]$req.volume; "
    "$ms = New-Object System.IO.MemoryStream; "
    "$s.SetOutputToWaveStream($ms); "
    "$s.Speak([string]$req.text); "  # Speak(string) = plain text; SpeakSsml is never used
    "$s.SetOutputToNull(); "
    "$b = $ms.ToArray(); $o = [Console]::OpenStandardOutput(); "
    "$o.Write($b, 0, $b.Length); $o.Flush()"
)


@dataclass(frozen=True)
class VoiceInfo:
    name: str  # e.g. "Microsoft Irina Desktop"
    culture: str  # e.g. "ru-RU"


# (argv, stdin bytes, timeout seconds) -> completed process; replaced by a fake in unit tests.
Runner = Callable[[Sequence[str], bytes, float], "subprocess.CompletedProcess[bytes]"]


def _powershell_path() -> str:
    # Never a bare name: CreateProcess would search the current directory first.
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _argv(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        _powershell_path(),
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def _default_runner(
    argv: Sequence[str], stdin: bytes, timeout_s: float
) -> subprocess.CompletedProcess[bytes]:
    # Minimal environment: the child needs nothing from ours (no PATH, no secrets/API keys).
    env = {k: v for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if (v := os.environ.get(k))}
    return subprocess.run(
        list(argv),  # fixed argv, no shell, no untrusted input
        env=env,
        input=stdin,
        capture_output=True,
        timeout=timeout_s,  # subprocess.run kills the child on timeout
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def select_voice(voices: Sequence[VoiceInfo], preferred: str, lang: str) -> VoiceInfo | None:
    """Pick a voice: case-insensitive substring of the name, else any voice of the language
    (culture prefix), else ``None`` (= the system default voice)."""
    wanted = preferred.strip().lower()
    if wanted:
        for v in voices:
            if wanted in v.name.lower():
                return v
    prefix = lang.strip().lower()
    for v in voices:
        if prefix and v.culture.lower().split("-")[0] == prefix:
            return v
    return None


def _clean(text: str) -> str:
    # Lone surrogates cannot be encoded as UTF-8 for the engine; the rest is passed unmodified.
    return text.encode("utf-8", "replace").decode("utf-8")


def _wav_to_clip(data: bytes) -> AudioClip:
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError, ValueError, OverflowError) as e:
        raise ProviderUnavailable(f"SAPI returned invalid audio: {e}") from e
    if width != SAMPLE_WIDTH_BYTES:
        raise ProviderUnavailable(f"SAPI returned {width * 8}-bit audio, expected 16-bit")
    if channels > 1:  # keep the first channel (mono contract of AudioClip)
        step = channels * width
        frames = b"".join(frames[i : i + width] for i in range(0, len(frames) - step + 1, step))
    if not frames:
        raise ProviderUnavailable("SAPI produced no audio")
    try:
        return AudioClip(pcm=frames, sample_rate=rate)
    except ValueError as e:
        raise ProviderUnavailable(f"SAPI returned unusable audio: {e}") from e


class SapiTTS:
    name = "sapi"

    def __init__(
        self,
        voice_ru: str = "Irina",
        voice_en: str = "Zira",
        rate: int = 0,
        volume: int = 100,
        *,
        timeout_s: float = 30.0,
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._voices_pref = {"ru": voice_ru, "en": voice_en}
        self._rate = max(-10, min(10, int(rate)))
        self._volume = max(0, min(100, int(volume)))
        self._timeout_s = timeout_s
        self._runner = runner or _default_runner
        self._lock = threading.Lock()
        self._voices: tuple[VoiceInfo, ...] | None = None
        self._clock = clock
        self._voices_failed_at: float | None = None

    def list_voices(self) -> tuple[VoiceInfo, ...]:
        """Installed, enabled SAPI voices (cached after the first call). For diagnostics."""
        with self._lock:
            if self._voices is None:
                self._voices = self._query_voices()
            return self._voices

    def synthesize(self, text: str, lang: str) -> AudioClip:
        if not text or not text.strip():
            raise ProviderRequestError("nothing to speak: empty text")
        if len(text) > MAX_TEXT_CHARS:
            raise ProviderRequestError(f"text too long to speak ({len(text)} > {MAX_TEXT_CHARS})")
        lang = lang.strip().lower()
        if lang not in self._voices_pref:
            raise ProviderRequestError(f"unsupported language: {lang!r}")
        voice = self._choose_voice(lang)
        request = json.dumps(  # ensure_ascii: stdin is pure ASCII, no codepage issues
            {
                "voice": voice.name if voice else None,
                "rate": self._rate,
                "volume": self._volume,
                "text": _clean(text),
            }
        ).encode("ascii")
        out = self._run(_argv(_SPEAK_SCRIPT), request)
        return _wav_to_clip(out)

    def _choose_voice(self, lang: str) -> VoiceInfo | None:
        with self._lock:
            failed_at = self._voices_failed_at
            recently_failed = failed_at is not None and self._clock() - failed_at < VOICE_RETRY_S
        if recently_failed:
            return None  # already warned; do not wait for another timeout on every reply
        try:
            voices = self.list_voices()
        except ProviderUnavailable as e:
            with self._lock:
                self._voices_failed_at = self._clock()
            log.warning("Cannot list SAPI voices (%s); using the system default voice", e)
            return None
        voice = select_voice(voices, self._voices_pref[lang], lang)
        if voice is None:
            log.warning(
                "No SAPI voice matches %r for language %r; using the system default voice. "
                "Installed voices: %s",
                self._voices_pref[lang],
                lang,
                ", ".join(f"{v.name} ({v.culture})" for v in voices) or "none",
            )
        return voice

    def _query_voices(self) -> tuple[VoiceInfo, ...]:
        raw = self._run(_argv(_LIST_SCRIPT), b"").decode("utf-8", "replace").strip()
        try:
            data = json.loads(raw) if raw else []
        except ValueError as e:
            raise ProviderUnavailable("SAPI voice list is not valid JSON") from e
        if isinstance(data, dict):  # a single voice may be serialized as an object
            data = [data]
        voices = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                culture = item.get("culture")
                voices.append(VoiceInfo(item["name"], culture if isinstance(culture, str) else ""))
        return tuple(voices)

    def _run(self, argv: Sequence[str], stdin: bytes) -> bytes:
        if sys.platform != "win32" and self._runner is _default_runner:
            raise ProviderUnavailable("SAPI voices are only available on Windows")
        try:
            proc = self._runner(argv, stdin, self._timeout_s)
        except subprocess.TimeoutExpired as e:
            raise ProviderUnavailable(f"SAPI synthesis timed out after {self._timeout_s}s") from e
        except OSError as e:
            raise ProviderUnavailable(f"cannot start PowerShell: {e}") from e
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip()[-_STDERR_TAIL:]
            raise ProviderUnavailable(f"SAPI synthesis failed (exit {proc.returncode}): {tail}")
        if len(proc.stdout) > MAX_OUTPUT_BYTES:
            raise ProviderUnavailable("SAPI output is unreasonably large")
        return proc.stdout
