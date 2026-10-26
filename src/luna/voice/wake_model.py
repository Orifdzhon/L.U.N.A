"""Lazy, verified download of the Vosk wake-word model (ADR 0004, slice 4).

``WakeModelStore.ensure()`` returns the model directory, downloading it on first use:

- the URL is pinned (official ``alphacephei.com`` host, exact file name) and so is the SHA-256 of
  the archive; a mismatch deletes the download and fails;
- the archive goes to a temp file inside the models dir (same volume, so the final rename is
  atomic) with a hard size cap, a per-request timeout and one global deadline;
- extraction is defensive (zip-slip): absolute paths, drive letters, ``..``, symlinks, a wrong
  top-level folder, too many entries and an oversized unpacked total are all rejected;
  the unpacked data is counted while it is written, not just taken from the archive headers;
- the model is unpacked into a temp directory and renamed into place, so a half-finished
  download never looks like an installed model. Calling ``ensure()`` again with the model present
  does no network access.

Every failure is a ``ProviderUnavailable`` with a user-presentable message. Nothing here runs at
import time except constants.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from luna.core.errors import ProviderUnavailable
from luna.stt.faster_whisper import default_model_dir

log = logging.getLogger(__name__)

MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
MODEL_ARCHIVE_BYTES = 46_236_750  # for the log line; the cap below is what is enforced

MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024  # real archive: ~44 MiB
MAX_UNPACKED_BYTES = 250 * 1024 * 1024  # real model: ~87 MiB
MAX_ENTRIES = 500  # real model: 20
CONNECT_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 30.0
DEADLINE_S = 900.0
CHUNK_BYTES = 256 * 1024
_TMP_PREFIX = ".wake-model-"
STALE_TMP_AGE_S = 3600.0
MODEL_MARKER = ("am", "final.mdl")  # a model folder without this file is treated as absent
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

ClientFactory = Callable[[], httpx.Client]
Clock = Callable[[], float]


def _default_client() -> httpx.Client:
    # No redirects and no environment proxies: the pinned URL is expected to answer directly.
    # (Known limit: users behind a mandatory corporate proxy cannot download the model; they
    # can unpack the official zip into the models folder by hand.)
    return httpx.Client(
        timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "LUNA-assistant (model download)", "Accept-Encoding": "identity"},
    )


class WakeModelStore:
    """Finds or downloads the Vosk model under ``model_dir`` (default: shared models dir).

    ``ensure()`` blocks (up to ``deadline_s``) and must be called from a worker thread, never
    from the asyncio loop. Environment proxies are ignored on purpose (``trust_env=False``).
    """

    def __init__(
        self,
        model_dir: Path | str | None = None,
        *,
        url: str = MODEL_URL,
        sha256: str = MODEL_SHA256,
        name: str = MODEL_NAME,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        max_unpacked_bytes: int = MAX_UNPACKED_BYTES,
        deadline_s: float = DEADLINE_S,
        client_factory: ClientFactory = _default_client,
        clock: Clock = time.monotonic,
    ) -> None:
        self._root = Path(model_dir) if model_dir else default_model_dir()
        self._url = url
        self._sha256 = sha256.lower()
        self._name = name
        self._max_download = max_download_bytes
        self._max_unpacked = max_unpacked_bytes
        self._deadline_s = deadline_s
        self._client_factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return self._root / self._name

    def is_present(self) -> bool:
        try:
            return self.model_path.joinpath(*MODEL_MARKER).is_file()
        except OSError:
            return False

    def ensure(self) -> Path:
        """Return the model directory, downloading and unpacking it first if needed."""
        with self._lock:
            if self.is_present():
                return self.model_path
            self._install()
            return self.model_path

    # -- installation ------------------------------------------------------------------------

    def _sweep_stale(self) -> None:
        """Remove temp dirs left by a killed run (older than an hour; never the live ones)."""
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

    def _install(self) -> None:
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
            archive = work / "model.zip"
            log.info(
                "downloading wake-word model %s (about %d MB, first use) into %s",
                self._name,
                MODEL_ARCHIVE_BYTES // (1024 * 1024),
                self._root,
            )
            self._download(archive)
            unpacked = work / "unpacked"
            self._extract(archive, unpacked)
            self._publish(unpacked / self._name, work)
        except ProviderUnavailable:
            raise
        except OSError as exc:
            raise ProviderUnavailable(
                f"could not install the wake-word model ({type(exc).__name__}): "
                f"check free disk space and write access to {self._root}"
            ) from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log.info("wake-word model ready at %s", self.model_path)

    def _publish(self, source: Path, work: Path) -> None:
        target = self.model_path
        if target.exists():
            # Not a valid model (ensure() checked), so it is junk: an empty folder or a
            # half-deleted model. os.replace(dir, existing dir) fails on Windows, so remove an
            # empty one with rmdir (never a recursive delete of user data) and move any other
            # aside into the temp dir, which is deleted afterwards.
            try:
                target.rmdir()
            except OSError:
                with contextlib.suppress(OSError):
                    os.replace(target, work / "stale")
        try:
            os.replace(source, target)  # atomic: same volume, target now absent
        except OSError:
            if self.is_present():  # another process won the race
                return
            raise

    def _download(self, dest: Path) -> None:
        digest = hashlib.sha256()
        total = 0
        started = self._clock()
        next_log = 10 * 1024 * 1024
        try:
            with (
                self._client_factory() as client,
                client.stream("GET", self._url) as response,
                dest.open("wb") as out,
            ):
                if response.status_code != 200:
                    raise ProviderUnavailable(
                        f"wake-word model download failed: HTTP {response.status_code}"
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self._max_download:
                    raise ProviderUnavailable("wake-word model download is larger than expected")
                for (
                    chunk
                ) in response.iter_bytes():  # one item per network read: deadline stays live
                    total += len(chunk)
                    if total > self._max_download:
                        raise ProviderUnavailable(
                            "wake-word model download is larger than expected; aborted"
                        )
                    if self._clock() - started > self._deadline_s:
                        raise ProviderUnavailable("wake-word model download timed out")
                    digest.update(chunk)
                    out.write(chunk)
                    if total >= next_log:
                        log.info("wake-word model: %d MB downloaded", total // (1024 * 1024))
                        next_log += 10 * 1024 * 1024
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"wake-word model download failed ({type(exc).__name__}): "
                "check the internet connection"
            ) from exc
        if digest.hexdigest() != self._sha256:
            raise ProviderUnavailable(
                "wake-word model download failed the SHA-256 check; the file was discarded"
            )
        log.info("wake-word model: %d bytes downloaded, SHA-256 verified", total)

    def _extract(self, archive: Path, dest: Path) -> None:
        try:
            with zipfile.ZipFile(archive) as zf:
                self._extract_members(zf, dest)
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            zlib.error,
            EOFError,
            NotImplementedError,
            RuntimeError,
        ) as exc:
            raise ProviderUnavailable("wake-word model archive is corrupt or unsupported") from exc
        except OSError as exc:
            raise ProviderUnavailable(
                f"could not unpack the wake-word model ({type(exc).__name__}): "
                "check free disk space"
            ) from exc
        if not (dest / self._name).is_dir():
            raise ProviderUnavailable(f"wake-word model archive has no {self._name!r} folder")

    def _extract_members(self, zf: zipfile.ZipFile, dest: Path) -> None:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ProviderUnavailable("wake-word model archive has too many entries")
        if sum(i.file_size for i in infos) > self._max_unpacked:
            raise ProviderUnavailable("wake-word model archive unpacks to too much data")
        dest.mkdir(parents=True)
        root = dest.resolve()
        written = 0
        for info in infos:
            parts = self._safe_parts(info)
            target = root.joinpath(*parts)
            if root not in target.resolve().parents and target.resolve() != root:
                raise ProviderUnavailable("wake-word model archive has an unsafe path")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                while chunk := src.read(CHUNK_BYTES):
                    written += len(chunk)
                    if written > self._max_unpacked:
                        raise ProviderUnavailable(
                            "wake-word model archive unpacks to too much data"
                        )
                    out.write(chunk)

    def _safe_parts(self, info: zipfile.ZipInfo) -> tuple[str, ...]:
        name = info.filename
        if "\\" in name or "\x00" in name or name.startswith("/") or ":" in name:
            raise ProviderUnavailable("wake-word model archive has an unsafe path")
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise ProviderUnavailable("wake-word model archive contains a symbolic link")
        parts = PurePosixPath(name).parts
        for part in parts:
            if part != part.rstrip(". ") or part.split(".")[0].lower() in _RESERVED_NAMES:
                raise ProviderUnavailable("wake-word model archive has an unsafe path")
        if not parts or any(p in ("..", ".") for p in parts) or parts[0] != self._name:
            raise ProviderUnavailable("wake-word model archive has an unsafe path")
        return parts
