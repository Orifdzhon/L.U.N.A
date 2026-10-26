"""Lazy, verified download of Piper voices packaged for sherpa-onnx (ADR 0005, amendment 3).

``PiperModelStore.ensure(voice)`` returns the voice directory, downloading it on first use.
It follows the wake-word model store (``luna.voice.wake_model``):

- URLs are pinned (the official sherpa-onnx release host, exact file names) together with the
  SHA-256 of each archive; a mismatch deletes the download and fails;
- the archive goes to a temp file inside the models dir (same volume, so the final rename is
  atomic) with a hard size cap, per-request timeouts and one global deadline;
- extraction is defensive (tar-slip): only regular files and directories are accepted (no
  symlinks, hard links or devices), absolute paths, drive letters, ``..``, reserved Windows
  names, a wrong top-level folder, too many entries and an oversized unpacked total are
  rejected. Every member also passes ``tarfile.data_filter``; the data is written by hand and
  counted while it is written, never taken from the headers alone;
- the voice is unpacked into a temp directory and renamed into place, so a half-finished
  download never looks like an installed voice. A second ``ensure()`` does no network access.

Every failure is a ``ProviderUnavailable`` with a user-presentable message.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from luna.core.errors import ProviderUnavailable
from luna.stt.faster_whisper import default_model_dir

log = logging.getLogger(__name__)

_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"

MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024  # real archives: ~64 MiB
MAX_UNPACKED_BYTES = 200 * 1024 * 1024  # real voices: ~78 MiB
MAX_ENTRIES = 1000  # real voices: 397
CONNECT_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 30.0
DEADLINE_S = 1200.0
CHUNK_BYTES = 256 * 1024
_TMP_PREFIX = ".piper-voice-"
STALE_TMP_AGE_S = 3600.0
_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


@dataclass(frozen=True)
class VoiceSpec:
    key: str  # e.g. "ru_RU-irina-medium"
    lang: str  # "ru" / "en"
    sha256: str
    archive_bytes: int  # for the log line; MAX_DOWNLOAD_BYTES is what is enforced

    @property
    def folder(self) -> str:
        return f"vits-piper-{self.key}"

    @property
    def url(self) -> str:
        return f"{_RELEASE}/{self.folder}.tar.bz2"

    @property
    def model_file(self) -> str:
        return f"{self.key}.onnx"


VOICES: dict[str, VoiceSpec] = {
    spec.key: spec
    for spec in (
        VoiceSpec(
            "ru_RU-irina-medium",
            "ru",
            "1fc0f54e5e084fe287c07909f2f6e0ba6d857864cf800e3ab80286a4e8233008",
            67_153_308,
        ),
        VoiceSpec(
            "en_US-kristin-medium",
            "en",
            "c2206f572df2956c50b1ae3367eebce3853c663e890cba8048cd62b1e4dbe6c7",
            67_259_230,
        ),
    )
}

ClientFactory = Callable[[], httpx.Client]
Clock = Callable[[], float]


def _default_client() -> httpx.Client:
    # GitHub release assets redirect to the object store, so redirects are followed; the pinned
    # SHA-256 makes the final host irrelevant for integrity. Environment proxies are ignored.
    return httpx.Client(
        timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        follow_redirects=True,
        max_redirects=5,
        trust_env=False,
        headers={"User-Agent": "LUNA-assistant (voice download)", "Accept-Encoding": "identity"},
    )


class PiperModelStore:
    """Finds or downloads Piper voices under ``model_dir`` (default: the shared models dir).

    ``ensure()`` blocks (up to ``deadline_s``) and must be called from a worker thread.
    """

    def __init__(
        self,
        model_dir: Path | str | None = None,
        *,
        voices: dict[str, VoiceSpec] | None = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        max_unpacked_bytes: int = MAX_UNPACKED_BYTES,
        max_entries: int = MAX_ENTRIES,
        deadline_s: float = DEADLINE_S,
        client_factory: ClientFactory = _default_client,
        clock: Clock = time.monotonic,
    ) -> None:
        self._root = Path(model_dir) if model_dir else default_model_dir()
        self._voices = VOICES if voices is None else voices
        self._max_download = max_download_bytes
        self._max_unpacked = max_unpacked_bytes
        self._max_entries = max_entries
        self._deadline_s = deadline_s
        self._client_factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()

    def _spec(self, voice: str) -> VoiceSpec:
        spec = self._voices.get(voice)
        if spec is None:
            raise ProviderUnavailable(f"unknown Piper voice {voice!r}")
        return spec

    def voice_path(self, voice: str) -> Path:
        return self._root / self._spec(voice).folder

    def is_present(self, voice: str) -> bool:
        spec = self._spec(voice)
        return self._complete(self._root / spec.folder, spec)

    @staticmethod
    def _complete(base: Path, spec: VoiceSpec) -> bool:
        try:
            return (
                (base / spec.model_file).is_file()
                and (base / "tokens.txt").is_file()
                and (base / "espeak-ng-data").is_dir()
            )
        except OSError:
            return False

    def ensure(self, voice: str) -> Path:
        """Return the voice directory, downloading and unpacking it first if needed."""
        spec = self._spec(voice)
        with self._lock:
            if not self.is_present(voice):
                self._install(spec)
            return self._root / spec.folder

    # -- installation ------------------------------------------------------------------------

    def _sweep_stale(self) -> None:
        cutoff = time.time() - STALE_TMP_AGE_S
        try:
            for entry in self._root.glob(f"{_TMP_PREFIX}*"):
                try:
                    if entry.is_dir() and entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            return

    def _install(self, spec: VoiceSpec) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._sweep_stale()
            work = Path(tempfile.mkdtemp(prefix=_TMP_PREFIX, dir=self._root))
        except OSError as exc:
            raise ProviderUnavailable(
                f"cannot create the models folder {self._root} ({type(exc).__name__}): "
                "check the path and free disk space"
            ) from exc
        try:
            archive = work / "voice.tar.bz2"
            log.info(
                "downloading voice %s (about %d MB, first use) into %s",
                spec.key,
                spec.archive_bytes // (1024 * 1024),
                self._root,
            )
            self._download(spec, archive)
            unpacked = work / "unpacked"
            self._extract(spec, archive, unpacked)
            if not self._complete(unpacked / spec.folder, spec):
                raise ProviderUnavailable(f"voice archive for {spec.key} lacks the expected files")
            self._publish(spec, unpacked / spec.folder, work)
        except ProviderUnavailable:
            raise
        except OSError as exc:
            raise ProviderUnavailable(
                f"could not install voice {spec.key} ({type(exc).__name__}): "
                f"check free disk space and write access to {self._root}"
            ) from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log.info("voice %s ready at %s", spec.key, self._root / spec.folder)

    def _publish(self, spec: VoiceSpec, source: Path, work: Path) -> None:
        target = self._root / spec.folder
        if target.exists():
            # Junk left by a broken install (is_present() was false): an empty folder or a
            # half-deleted voice. os.replace(dir, existing dir) fails on Windows, so remove an
            # empty one with rmdir and move any other aside into the temp dir (deleted later).
            try:
                target.rmdir()
            except OSError:
                with contextlib.suppress(OSError):
                    os.replace(target, work / "stale")
        try:
            os.replace(source, target)
        except OSError:
            if self.is_present(spec.key):  # another process won the race
                return
            raise

    def _download(self, spec: VoiceSpec, dest: Path) -> None:
        digest = hashlib.sha256()
        total = 0
        started = self._clock()
        next_log = 10 * 1024 * 1024
        try:
            with (
                self._client_factory() as client,
                client.stream("GET", spec.url) as response,
                dest.open("wb") as out,
            ):
                if response.status_code != 200:
                    raise ProviderUnavailable(f"voice download failed: HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self._max_download:
                    raise ProviderUnavailable("voice download is larger than expected")
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self._max_download:
                        raise ProviderUnavailable("voice download is larger than expected; aborted")
                    if self._clock() - started > self._deadline_s:
                        raise ProviderUnavailable("voice download timed out")
                    digest.update(chunk)
                    out.write(chunk)
                    if total >= next_log:
                        log.info(
                            "voice %s: %d of about %d MB downloaded",
                            spec.key,
                            total // (1024 * 1024),
                            spec.archive_bytes // (1024 * 1024),
                        )
                        next_log += 10 * 1024 * 1024
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"voice download failed ({type(exc).__name__}): check the internet connection"
            ) from exc
        if digest.hexdigest() != spec.sha256.lower():
            raise ProviderUnavailable("voice download failed the SHA-256 check; file discarded")
        log.info("voice %s: %d bytes downloaded, SHA-256 verified", spec.key, total)

    def _extract(self, spec: VoiceSpec, archive: Path, dest: Path) -> None:
        try:
            with tarfile.open(archive, mode="r|bz2") as tf:
                self._extract_members(spec, tf, dest)
        except ProviderUnavailable:
            raise
        except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
            # OSError also covers bz2 "Invalid data stream"; disk errors are reported the same.
            raise ProviderUnavailable(
                f"voice archive is corrupt or could not be unpacked ({type(exc).__name__})"
            ) from exc
        if not (dest / spec.folder).is_dir():
            raise ProviderUnavailable(f"voice archive has no {spec.folder!r} folder")

    def _extract_members(self, spec: VoiceSpec, tf: tarfile.TarFile, dest: Path) -> None:
        dest.mkdir(parents=True)
        root = dest.resolve()
        count = 0
        written = 0
        for member in tf:
            count += 1
            if count > self._max_entries:
                raise ProviderUnavailable("voice archive has too many entries")
            parts = self._safe_parts(spec, member)
            try:
                tarfile.data_filter(member, str(root))
            except tarfile.FilterError as exc:
                raise ProviderUnavailable("voice archive has an unsafe member") from exc
            target = root.joinpath(*parts)
            resolved = target.resolve()
            if root not in resolved.parents and resolved != root:
                raise ProviderUnavailable("voice archive has an unsafe path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if written + member.size > self._max_unpacked:
                raise ProviderUnavailable("voice archive unpacks to too much data")
            source = tf.extractfile(member)
            if source is None:
                raise ProviderUnavailable("voice archive has an unreadable member")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as out:
                while chunk := source.read(CHUNK_BYTES):
                    written += len(chunk)
                    if written > self._max_unpacked:
                        raise ProviderUnavailable("voice archive unpacks to too much data")
                    out.write(chunk)

    @staticmethod
    def _safe_parts(spec: VoiceSpec, member: tarfile.TarInfo) -> tuple[str, ...]:
        name = member.name
        if "\\" in name or "\x00" in name or name.startswith("/") or ":" in name:
            raise ProviderUnavailable("voice archive has an unsafe path")
        if not (member.isfile() or member.isdir()):
            raise ProviderUnavailable("voice archive contains a link or special file")
        parts = PurePosixPath(name).parts
        for part in parts:
            if part != part.rstrip(". ") or part.split(".")[0].lower() in _RESERVED_NAMES:
                raise ProviderUnavailable("voice archive has an unsafe path")
        if not parts or any(p in ("..", ".") for p in parts) or parts[0] != spec.folder:
            raise ProviderUnavailable("voice archive has an unsafe path")
        return parts
