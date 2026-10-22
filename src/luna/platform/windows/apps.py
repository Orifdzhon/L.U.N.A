"""Windows app catalog (``Get-StartApps``) and launcher (``shell:AppsFolder``).

Design notes (ADR 0008):

* Discovery runs PowerShell with a **fixed argument list** (no ``shell=True``, no text from
  the user or the LLM is ever interpolated into it), a timeout, and forces UTF-8 output so
  Cyrillic names survive. The JSON is parsed defensively; entries with a malformed name or
  an unsafe AppID are dropped.
* Failure policy: ``refresh()`` keeps the previous snapshot (memory or on-disk cache) and
  logs a warning when PowerShell fails or times out; it raises ``ProviderUnavailable`` only
  when there is nothing cached to fall back on. ``list_apps()`` is served from the in-memory
  snapshot, then from the cache file, and scans only if both are empty.
* Launching uses ``os.startfile("shell:AppsFolder\\<AppID>")`` (ShellExecute "open" verb with
  one argument; no command line is built, no arguments are passed). It is chosen over
  spawning ``explorer.exe`` because it needs no subprocess and no quoting. The AppID must come
  from an ``AppEntry`` that is currently in the catalog and must pass ``is_safe_app_id``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from luna.core.errors import ProviderUnavailable, ToolError
from luna.platform.base import AppCatalog, AppEntry

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20.0
CACHE_FILENAME = "apps.json"
_CACHE_VERSION = 1
_MAX_ID_CHARS = 512
_MAX_NAME_CHARS = 256

# Output is forced to UTF-8 so non-ASCII names do not depend on the console code page.
_POWERSHELL_SCRIPT = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-StartApps | ConvertTo-Json -Compress"
)

# (argv, timeout_s) -> completed process with bytes stdout/stderr. Injectable for tests.
Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[bytes]"]
StartFile = Callable[[str], object]

_UNSAFE_ID_CHARS = frozenset("\"<>|&^%;`$*?'/")


def is_safe_app_id(app_id: object) -> bool:
    """True for an AppUserModelID / Start-menu id that is safe to append to ``AppsFolder``.

    Real ids look like ``Chrome``, ``Microsoft.WindowsCalculator_8wekyb3d8bbwe!App`` or
    ``{1AC14E77-...}\\notepad.exe``. Rejected: empty/oversized ids, control characters,
    leading/trailing whitespace, quotes, shell metacharacters, ``%`` (env expansion), ``/``,
    ``..`` path segments and anything that already looks like a ``shell:`` URI.
    """
    if not isinstance(app_id, str) or not app_id or len(app_id) > _MAX_ID_CHARS:
        return False
    if app_id != app_id.strip() or not app_id.isprintable():
        return False
    if any(ch in _UNSAFE_ID_CHARS for ch in app_id):
        return False
    return ".." not in app_id.split("\\") and not app_id.lower().startswith("shell:")


def _powershell_path() -> str:
    root = os.environ.get("SYSTEMROOT")
    if root:
        candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return "powershell.exe"


def powershell_argv() -> list[str]:
    return [
        _powershell_path(),
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _POWERSHELL_SCRIPT,
    ]


def _default_runner(argv: Sequence[str], timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        list(argv),  # fixed argv, no shell, no untrusted input
        capture_output=True,
        timeout=timeout_s,
        check=False,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\LUNA`` (falls back to ``~/AppData/Local/LUNA`` if unset)."""
    base = (env if env is not None else os.environ).get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "LUNA"


def _to_entry(item: object) -> AppEntry | None:
    if not isinstance(item, dict):
        return None
    name, app_id = item.get("Name"), item.get("AppID")
    if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME_CHARS:
        return None
    if not name.isprintable() or not isinstance(app_id, str) or not is_safe_app_id(app_id):
        return None
    return AppEntry(name=name.strip(), app_id=app_id)


def parse_start_apps(raw: bytes | str) -> tuple[AppEntry, ...]:
    """Parse ``Get-StartApps | ConvertTo-Json`` output.

    Handles empty output, a single object instead of an array, a UTF-8 BOM, and drops
    malformed entries. Raises ``ValueError`` if the text is not valid JSON of the expected
    shape.
    """
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw.lstrip("﻿")
    if not text.strip():
        return ()
    data: Any = json.loads(text)
    if data is None:
        return ()
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("unexpected JSON shape from Get-StartApps")
    seen: set[str] = set()
    entries: list[AppEntry] = []
    for item in data:
        entry = _to_entry(item)
        if entry is None:
            log.warning("skipping malformed Get-StartApps entry")
            continue
        if entry.app_id in seen:
            continue
        seen.add(entry.app_id)
        entries.append(entry)
    return tuple(entries)


class WindowsAppCatalog:
    """``AppCatalog`` backed by ``Get-StartApps`` with an on-disk JSON cache."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        runner: Runner | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._cache_file = (cache_dir or default_cache_dir()) / CACHE_FILENAME
        self._runner = runner or _default_runner
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._snapshot: tuple[AppEntry, ...] | None = None

    def list_apps(self) -> tuple[AppEntry, ...]:
        snap = self._snapshot
        if snap is not None:
            return snap
        with self._lock:
            if self._snapshot is None:
                cached = self._read_cache()
                if cached is not None:
                    self._snapshot = cached
                else:
                    self._scan_locked()
            snap = self._snapshot
            assert snap is not None
            return snap

    def refresh(self) -> None:
        with self._lock:
            self._scan_locked()

    # -- internals -----------------------------------------------------------------------

    def _scan_locked(self) -> None:
        try:
            entries = self._scan()
        except ProviderUnavailable as exc:
            fallback = self._snapshot if self._snapshot is not None else self._read_cache()
            if fallback is None:
                raise
            log.warning("app scan failed, keeping cached catalog: %s", exc)
            self._snapshot = fallback
            return
        self._write_cache(entries)
        self._snapshot = entries  # single assignment: readers see the old or the new tuple

    def _scan(self) -> tuple[AppEntry, ...]:
        try:
            proc = self._runner(powershell_argv(), self._timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnavailable(f"Get-StartApps timed out after {self._timeout_s}s") from exc
        except OSError as exc:
            raise ProviderUnavailable(f"cannot run PowerShell: {exc}") from exc
        if proc.returncode != 0:
            raise ProviderUnavailable(f"Get-StartApps failed with exit code {proc.returncode}")
        try:
            return parse_start_apps(proc.stdout)
        except ValueError as exc:  # includes JSONDecodeError and UnicodeDecodeError
            raise ProviderUnavailable(f"cannot parse Get-StartApps output: {exc}") from exc

    def _read_cache(self) -> tuple[AppEntry, ...] | None:
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if data.get("version") != _CACHE_VERSION:
                return None
            raw = ({"Name": a["name"], "AppID": a["app_id"]} for a in data["apps"])
            return tuple(e for e in map(_to_entry, raw) if e is not None)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _write_cache(self, entries: Sequence[AppEntry]) -> None:
        payload = {
            "version": _CACHE_VERSION,
            "apps": [{"name": e.name, "app_id": e.app_id} for e in entries],
        }
        tmp_name: str | None = None
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._cache_file.parent, prefix="apps-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_name, self._cache_file)
        except OSError as exc:  # the cache is an optimization, never fatal
            log.warning("cannot write app cache %s: %s", self._cache_file, exc)
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)


class WindowsAppLauncher:
    """``AppLauncher`` that starts catalog entries via ``shell:AppsFolder\\<AppID>``."""

    def __init__(self, catalog: AppCatalog, startfile: StartFile | None = None) -> None:
        self._catalog = catalog
        self._startfile = startfile or getattr(os, "startfile", None)

    def launch(self, app: AppEntry) -> None:
        if not isinstance(app, AppEntry) or app not in self._catalog.list_apps():
            raise ToolError("app is not in the catalog")
        if not is_safe_app_id(app.app_id):
            raise ToolError(f"unsafe app id for {app.name!r}")
        if self._startfile is None:
            raise ToolError("launching apps is only supported on Windows")
        try:
            self._startfile("shell:AppsFolder\\" + app.app_id)
        except OSError as exc:
            raise ToolError(f"cannot launch {app.name!r}: {exc}") from exc
