"""Windows side of the file search: known folders, fixed drives and the Explorer opener.

``ShellFileOpener.open`` is ``os.startfile`` (ShellExecute "open", no arguments); whether a file
type may be opened at all is decided by the caller (``open_file``, ADR 0011 D7).
``reveal`` uses ``SHOpenFolderAndSelectItems`` and falls back to ``explorer.exe /select,``
started from ``%SystemRoot%`` without a shell.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import uuid
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

from luna.core.errors import ToolError

log = logging.getLogger(__name__)

DRIVE_FIXED = 3
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

# label -> Known Folder id (so redirected / OneDrive-moved folders are found)
KNOWN_FOLDERS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def from_string(cls, text: str) -> _GUID:
        u = uuid.UUID(text)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


def known_folders() -> list[tuple[str, Path]]:
    """Desktop, Documents, Downloads, Music, Pictures, Videos as Windows reports them."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    get_path = shell32.SHGetKnownFolderPath
    get_path.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p]
    get_path.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    out: list[tuple[str, Path]] = []
    for label, guid in KNOWN_FOLDERS.items():
        pointer = ctypes.c_void_p()
        try:
            if get_path(ctypes.byref(_GUID.from_string(guid)), 0, None, ctypes.byref(pointer)) == 0:
                text = ctypes.wstring_at(pointer)
                if text:
                    out.append((label, Path(text)))
        except OSError:
            log.debug("known folder lookup failed", exc_info=True)
        finally:
            if pointer.value:
                ole32.CoTaskMemFree(pointer)
    return out


def fixed_drives() -> list[Path]:
    """Every fixed drive (no removable, network or optical drives)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    mask = kernel32.GetLogicalDrives()
    drives: list[Path] = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(ord('A') + i)}:\\"
            if kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
                drives.append(Path(root))
    return drives


def _shell_select(path: str) -> bool:
    """Select ``path`` in an Explorer window; False if the shell call failed."""
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    try:
        pidl = shell32.ILCreateFromPathW(path)
        if not pidl:
            return False
        try:
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
    finally:
        if init in (0, 1):  # S_OK / S_FALSE: balanced; RPC_E_CHANGED_MODE: do not uninitialize
            ole32.CoUninitialize()


def _explorer_exe() -> str:
    return os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "explorer.exe")


class ShellFileOpener:
    def __init__(
        self,
        *,
        startfile: Callable[[str], None] | None = None,
        select: Callable[[str], bool] = _shell_select,
        spawn: Callable[[list[str]], object] | None = None,
    ) -> None:
        self._startfile = startfile or os.startfile  # type: ignore[attr-defined,unused-ignore]
        self._select = select
        self._spawn = spawn or (lambda argv: subprocess.Popen(argv, shell=False))

    def open(self, path: Path) -> None:
        try:
            self._startfile(str(path))
        except OSError as exc:
            raise ToolError(f"could not open the item ({type(exc).__name__})") from exc

    def reveal(self, path: Path) -> None:
        try:
            if self._select(str(path)):
                return
        except Exception:
            log.debug("SHOpenFolderAndSelectItems failed", exc_info=True)
        try:
            self._spawn([_explorer_exe(), "/select,", str(path)])
        except OSError as exc:
            raise ToolError(f"could not show the item ({type(exc).__name__})") from exc
