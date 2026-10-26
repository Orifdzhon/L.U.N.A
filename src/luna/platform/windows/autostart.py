"""Autostart through a shortcut in the user's Startup folder (ADR 0009; no registry).

The shortcut is written by PowerShell (``WScript.Shell``) running a FIXED script. The lnk
path, target, arguments and working directory reach it only through environment variables of
the child process, so nothing is interpolated into the script text.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from luna.core.errors import ToolError
from luna.platform.base import LaunchCommand

log = logging.getLogger(__name__)

SHORTCUT_NAME = "L.U.N.A.lnk"
WRITE_TIMEOUT_S = 10.0

_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LUNA_LNK); "
    "$s.TargetPath = $env:LUNA_TARGET; "
    "$s.Arguments = $env:LUNA_ARGS; "
    "$s.WorkingDirectory = $env:LUNA_CWD; "
    "$s.Description = 'L.U.N.A. voice assistant'; "
    "$s.Save()"
)

ShortcutWriter = Callable[[Path, LaunchCommand], None]


def _known_startup_folder() -> Path | None:
    """``SHGetKnownFolderPath(FOLDERID_Startup)``; None when unavailable."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _Guid(ctypes.Structure):
            _fields_ = (
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            )

        guid = uuid.UUID("{B97D20BB-F46A-4C97-BA10-5E3608430854}")  # FOLDERID_Startup
        g = _Guid.from_buffer_copy(guid.bytes_le)
        out = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(_Guid),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
        try:
            if hr == 0 and out.value:
                return Path(out.value)
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:  # any failure falls back to %APPDATA%
        log.debug("SHGetKnownFolderPath failed", exc_info=True)
    return None


def default_startup_dir() -> Path:
    known = _known_startup_folder()
    if known is not None:
        return known
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def powershell_writer(lnk: Path, command: LaunchCommand) -> None:
    """Create/overwrite ``lnk`` by running the fixed PowerShell script."""
    env = dict(os.environ)
    env["LUNA_LNK"] = str(lnk)
    env["LUNA_TARGET"] = command.executable
    env["LUNA_ARGS"] = subprocess.list2cmdline(list(command.args))
    env["LUNA_CWD"] = command.working_dir or ""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=WRITE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError("creating the autostart shortcut timed out") from exc
    except OSError as exc:
        raise ToolError(f"cannot run PowerShell to create the shortcut: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        raise ToolError(f"creating the autostart shortcut failed: {detail or proc.returncode}")


class StartupFolderAutostart:
    """``Autostart`` implementation: one ``L.U.N.A.lnk`` in the Startup folder."""

    def __init__(
        self,
        startup_dir: Path | None = None,
        writer: ShortcutWriter | None = None,
    ) -> None:
        self._dir = startup_dir
        self._writer = writer or powershell_writer

    @property
    def shortcut_path(self) -> Path:
        if self._dir is None:
            self._dir = default_startup_dir()
        return self._dir / SHORTCUT_NAME

    def is_enabled(self) -> bool:
        try:
            return self.shortcut_path.is_file()
        except Exception:  # contract: never raises
            return False

    def enable(self, command: LaunchCommand) -> None:
        if not command.executable.strip():
            raise ToolError("autostart needs a non-empty executable")
        lnk = self.shortcut_path
        try:
            lnk.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"cannot prepare the Startup folder: {exc}") from exc
        try:
            self._writer(lnk, command)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"creating the autostart shortcut failed: {exc}") from exc

    def disable(self) -> None:
        try:
            self.shortcut_path.unlink(missing_ok=True)
        except OSError as exc:
            raise ToolError(f"cannot remove the autostart shortcut: {exc}") from exc
