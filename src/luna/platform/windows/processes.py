"""Running apps on Windows: list, close gracefully, terminate (ADR 0011, feature A).

An *app* is every visible, unowned, titled top-level window whose process has the same image
path (Chrome's many windows/processes are one app). Packaged (UWP) apps show a frame window of
``ApplicationFrameHost.exe``; the real app is the process of its ``Windows.UI.Core.CoreWindow``
child, and ``WM_CLOSE`` still goes to the frame.

Safety rules, enforced here and not in the tool:

* protected processes never appear in ``list_running`` and are never closed or killed: anything
  under ``%SystemRoot%`` except a small allowlist of user apps, a name denylist (shell, security
  software, ...), L.U.N.A.'s own process and its parent, processes that cannot be opened with
  ``PROCESS_QUERY_LIMITED_INFORMATION`` (other user, elevated) and processes of another session;
* window titles are never read (only "has a title" is checked to skip helper windows);
* ``terminate`` re-validates the process creation time right before acting (pid reuse).

``ctypes`` is touched only inside the ``_User32`` / ``_Kernel32`` / ``_Version`` / ``_Shell``
wrappers, lazily, so the module imports on any OS; tests replace the wrappers with fakes.
"""

from __future__ import annotations

import logging
import ntpath
import os
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from luna.core.errors import ToolError
from luna.platform.base import AppEntry, CloseOutcome, ProtectedApp, RunningApp
from luna.platform.windows.apps import classify_app_id

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.1
HUNG_PROBE_MS = 1000
FRAME_CLASS = "ApplicationFrameWindow"
CORE_WINDOW_CLASS = "Windows.UI.Core.CoreWindow"

# Under %SystemRoot% but ordinary user apps.
SYSTEM_ALLOWLIST = frozenset(
    ["notepad", "mspaint", "write", "charmap", "snippingtool", "mstsc", "magnify"]
)
# Protected by name wherever the exe lives (copies outside System32 included).
PROTECTED_NAMES = frozenset(
    [
        "explorer", "csrss", "winlogon", "wininit", "services", "lsass", "smss", "svchost",
        "dwm", "sihost", "fontdrvhost", "ctfmon", "taskhostw", "runtimebroker",
        "startmenuexperiencehost", "shellexperiencehost", "searchhost", "searchapp",
        "textinputhost", "lockapp", "applicationframehost", "securityhealthsystray",
        "securityhealthservice", "msmpeng", "nissrv", "mpdefendercoreservice", "smartscreen",
        "audiodg", "spoolsv", "conhost", "logonui", "userinit", "taskmgr",
        "avp", "avpui", "ekrn", "egui", "bdagent", "vsserv", "mcshield", "avastui", "avgui",
        "mbam", "mbamservice",
    ]
)  # fmt: skip


def _norm_path(path: str) -> str:
    """Case-folded, normalized path with the ``\\\\?\\`` and ``\\\\?\\UNC\\`` prefixes removed."""
    if path[:8].upper() == "\\\\?\\UNC\\":
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def _stem(path: str) -> str:
    return ntpath.splitext(ntpath.basename(path))[0]


def is_protected_path(path: str, system_root: str | None, extra: Iterable[str] = ()) -> bool:
    """Pure rule: is the exe at ``path`` off limits by location or name?"""
    stem = _stem(path).casefold()
    if stem in PROTECTED_NAMES or stem in {_stem(x).casefold() for x in extra}:
        return True
    if system_root:
        root = _norm_path(system_root).rstrip("\\") + "\\"
        if _norm_path(path).startswith(root) and stem not in SYSTEM_ALLOWLIST:
            return True
    return False


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    class_name: str
    visible: bool
    owned: bool  # has an owner window (dialogs, tool windows)
    titled: bool  # non-empty title; the title text itself is never read
    cloaked: bool  # hidden by the shell (suspended UWP, other virtual desktop)


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    image_path: str
    creation: int  # FILETIME of the process start
    session: int
    aumid: str | None


# ---------------------------------------------------------------------------------------------
# ctypes wrappers (replaced by fakes in tests)
# ---------------------------------------------------------------------------------------------


class _User32:
    WM_CLOSE = 0x0010
    WM_NULL = 0x0000
    GW_OWNER = 4
    SMTO_ABORTIFHUNG = 0x0002
    DWMWA_CLOAKED = 14

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = (self._enum_proc, wintypes.LPARAM)
        u.EnumChildWindows.argtypes = (wintypes.HWND, self._enum_proc, wintypes.LPARAM)
        u.IsWindowVisible.argtypes = (wintypes.HWND,)
        u.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        u.GetWindow.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.IsHungAppWindow.argtypes = (wintypes.HWND,)
        u.SendMessageTimeoutW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        )
        self._u = u
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmGetWindowAttribute.argtypes = (
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            self._dwm: Any = dwm
        except OSError:
            self._dwm = None

    def _info(self, hwnd: int) -> WindowInfo:
        ct, wt = self._ct, self._wt
        pid = wt.DWORD(0)
        self._u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        buf = ct.create_unicode_buffer(256)
        self._u.GetClassNameW(hwnd, buf, 256)
        cloaked = ct.c_int(0)
        if self._dwm is not None:
            self._dwm.DwmGetWindowAttribute(
                hwnd, self.DWMWA_CLOAKED, ct.byref(cloaked), ct.sizeof(cloaked)
            )
        return WindowInfo(
            hwnd=int(hwnd),
            pid=int(pid.value),
            class_name=buf.value,
            visible=bool(self._u.IsWindowVisible(hwnd)),
            owned=bool(self._u.GetWindow(hwnd, self.GW_OWNER)),
            titled=self._u.GetWindowTextLengthW(hwnd) > 0,
            cloaked=cloaked.value != 0,
        )

    def _enum(self, fn: Any, *args: Any) -> list[int]:
        found: list[int] = []

        def callback(hwnd: int, _lparam: int) -> bool:
            found.append(int(hwnd))
            return True

        fn(*args, self._enum_proc(callback), 0)
        return found

    def windows(self) -> list[WindowInfo]:
        """All top-level windows."""
        return [self._info(h) for h in self._enum(self._u.EnumWindows)]

    def children(self, hwnd: int) -> list[WindowInfo]:
        return [self._info(h) for h in self._enum(self._u.EnumChildWindows, hwnd)]

    def post_close(self, hwnd: int) -> None:
        self._u.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)

    def is_hung(self, hwnd: int) -> bool:
        """Hung per the shell, or not answering ``WM_NULL`` within a second."""
        if self._u.IsHungAppWindow(hwnd):
            return True
        result = self._ct.c_size_t(0)
        ok = self._u.SendMessageTimeoutW(
            hwnd, self.WM_NULL, 0, 0, self.SMTO_ABORTIFHUNG, HUNG_PROBE_MS, self._ct.byref(result)
        )
        return not ok


class _Kernel32:
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TH32CS_SNAPPROCESS = 0x2
    STILL_ACTIVE = 259
    INVALID_HANDLE_VALUE = None  # c_void_p(-1).value, set in __init__ (64-bit safe)

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        k.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
        k.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        k.GetApplicationUserModelId.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.UINT),
            wintypes.LPWSTR,
        )
        k.GetApplicationUserModelId.restype = ctypes.c_long
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.GetSystemWindowsDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
        k.GetSystemWindowsDirectoryW.restype = wintypes.UINT
        self.INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._k = k

        class ProcessEntry(ctypes.Structure):
            _fields_ = (
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            )

        self._entry = ProcessEntry
        k.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        k.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))

    def current_pid(self) -> int:
        return os.getpid()

    def session_of(self, pid: int) -> int | None:
        session = self._wt.DWORD(0)
        if not self._k.ProcessIdToSessionId(pid, self._ct.byref(session)):
            return None
        return int(session.value)

    def _creation(self, handle: Any) -> int | None:
        ft = [self._wt.FILETIME() for _ in range(4)]
        if not self._k.GetProcessTimes(handle, *(self._ct.byref(f) for f in ft)):
            return None
        return (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime

    def process_info(self, pid: int) -> ProcInfo | None:
        """None if the process cannot be opened for limited queries (other user, elevated)."""
        ct, wt = self._ct, self._wt
        handle = self._k.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wt.DWORD(1024)
            buf = ct.create_unicode_buffer(1024)
            if not self._k.QueryFullProcessImageNameW(handle, 0, buf, ct.byref(size)):
                return None
            exit_code = self._wt.DWORD(0)
            if not self._k.GetExitCodeProcess(handle, ct.byref(exit_code)) or (
                exit_code.value != self.STILL_ACTIVE
            ):
                return None  # already exited (a lingering handle keeps the object openable)
            creation = self._creation(handle)
            session = self.session_of(pid)
            if creation is None or session is None:
                return None
            n = wt.UINT(256)
            abuf = ct.create_unicode_buffer(256)
            rc = self._k.GetApplicationUserModelId(handle, ct.byref(n), abuf)
            return ProcInfo(pid, buf.value, creation, session, abuf.value if rc == 0 else None)
        finally:
            self._k.CloseHandle(handle)

    def process_table(self) -> dict[int, int]:
        """pid -> parent pid for every process (Toolhelp32 snapshot)."""
        snap = self._k.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        table: dict[int, int] = {}
        if not snap or snap == self.INVALID_HANDLE_VALUE:
            return table  # empty: the caller refuses to act without a process table
        try:
            entry = self._entry()
            entry.dwSize = self._ct.sizeof(entry)
            ok = self._k.Process32FirstW(snap, self._ct.byref(entry))
            while ok:
                table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = self._k.Process32NextW(snap, self._ct.byref(entry))
        finally:
            self._k.CloseHandle(snap)
        return table

    def system_windows_dir(self) -> str | None:
        """``GetSystemWindowsDirectoryW`` (the real %SystemRoot%, not an environment variable)."""
        buf = self._ct.create_unicode_buffer(260)
        n = self._k.GetSystemWindowsDirectoryW(buf, 260)
        return buf.value if 0 < n < 260 else None

    def terminate(self, pid: int, creation: int) -> bool:
        """TerminateProcess, but only if the process still started at ``creation``."""
        handle = self._k.OpenProcess(
            self.PROCESS_TERMINATE | self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            if self._creation(handle) != creation:
                return False
            return bool(self._k.TerminateProcess(handle, 1))
        finally:
            self._k.CloseHandle(handle)


class _Version:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        v = ctypes.WinDLL("version", use_last_error=True)
        v.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
        v.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        v.GetFileVersionInfoW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        v.VerQueryValueW.argtypes = (
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        )
        self._v = v

    def _query(self, data: Any, sub_block: str) -> tuple[int, int] | None:
        ptr = self._ct.c_void_p()
        length = self._wt.UINT(0)
        if (
            not self._v.VerQueryValueW(data, sub_block, self._ct.byref(ptr), self._ct.byref(length))
            or not ptr.value
        ):
            return None
        return int(ptr.value), int(length.value)

    def description(self, path: str) -> str | None:
        """``FileDescription`` of the exe (localized when the file is), or None."""
        ct = self._ct
        size = self._v.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ct.create_string_buffer(size)
        if not self._v.GetFileVersionInfoW(path, 0, size, data):
            return None
        langs = ["040904B0", "04090000"]
        tr = self._query(data, "\\VarFileInfo\\Translation")
        if tr and tr[1] >= 4:
            lang, codepage = (
                ct.c_ushort.from_address(tr[0]).value,
                ct.c_ushort.from_address(tr[0] + 2).value,
            )
            langs.insert(0, f"{lang:04X}{codepage:04X}")
        for code in langs:
            found = self._query(data, f"\\StringFileInfo\\{code}\\FileDescription")
            if found:
                text = ct.wstring_at(found[0], max(found[1] - 1, 0)).strip()
                if text:
                    return text
        return None


class _Shell:
    def known_folder(self, guid: str) -> str | None:
        """Path of a Known Folder given as ``{GUID}``, or None."""
        import ctypes
        from ctypes import wintypes

        try:
            raw = uuid.UUID(guid).bytes_le
        except ValueError:
            return None
        folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(raw)
        shell = ctypes.WinDLL("shell32")
        shell.SHGetKnownFolderPath.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p),
        )
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        out = ctypes.c_void_p()
        rc = shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out))
        try:
            return ctypes.wstring_at(out.value) if rc == 0 and out.value else None
        finally:
            ctypes.WinDLL("ole32").CoTaskMemFree(ctypes.c_void_p(out.value))


# ---------------------------------------------------------------------------------------------
# implementation
# ---------------------------------------------------------------------------------------------


@dataclass
class _Group:
    """Internal: one app with the handles needed to act on it."""

    image_path: str
    aumid: str | None
    procs: dict[int, ProcInfo]  # real app pids (UWP: the CoreWindow process)
    hwnds: list[int]  # visible, unowned windows to send WM_CLOSE to

    def first(self) -> ProcInfo:
        return self.procs[min(self.procs)]


def _make_key(info: ProcInfo) -> str:
    return f"{info.image_path}|{info.pid}|{info.creation}"


def _parse_key(key: str) -> tuple[str, int, int] | None:
    path, _, rest = key.rpartition("|")
    path, _, pid = path.rpartition("|")
    try:
        return path, int(pid), int(rest)
    except ValueError:
        return None


class WindowsRunningApps:
    """``RunningApps`` over Win32 (no pywin32, no PowerShell)."""

    def __init__(
        self,
        user32: Any = None,
        kernel32: Any = None,
        version: Any = None,
        shell: Any = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        protected_extra: Sequence[str] = (),
        system_root: str | None = None,
    ) -> None:
        self._user32 = user32
        self._kernel32 = kernel32
        self._version = version
        self._shell = shell
        self._clock = clock
        self._sleep = sleep
        self._extra = tuple(protected_extra)
        self._system_root = system_root
        self._descriptions: dict[str, str | None] = {}

    # lazy wrappers so the module imports (and tests construct) without ctypes
    @property
    def u(self) -> Any:
        if self._user32 is None:
            self._user32 = _User32()
        return self._user32

    @property
    def k(self) -> Any:
        if self._kernel32 is None:
            self._kernel32 = _Kernel32()
        return self._kernel32

    @property
    def v(self) -> Any:
        if self._version is None:
            self._version = _Version()
        return self._version

    @property
    def sh(self) -> Any:
        if self._shell is None:
            self._shell = _Shell()
        return self._shell

    # --- scanning ---------------------------------------------------------------------------

    def _root(self) -> str:
        """%SystemRoot%: the environment variable, else the API. Unknown -> refuse (fail closed)."""
        if not self._system_root:
            self._system_root = os.environ.get("SYSTEMROOT") or self.k.system_windows_dir()
        if not self._system_root:
            raise ToolError("cannot determine the Windows directory")
        return self._system_root

    def _protected(self, path: str) -> bool:
        return is_protected_path(path, self._root(), self._extra)

    def _table(self) -> dict[int, int]:
        """pid -> parent pid. An empty table, or one without our own process, means the
        snapshot failed: refuse to act rather than miss our own parent."""
        table = self.k.process_table()
        if not table or self.k.current_pid() not in table:
            raise ToolError("cannot read the process list")
        return table

    def _own_pids(self) -> set[int]:
        own = self.k.current_pid()
        parent = self._table().get(own)
        return {own} | ({parent} if parent else set())

    def _real_pid(self, win: WindowInfo) -> int | None:
        """The app's process: UWP frames resolve to their CoreWindow child."""
        if win.class_name != FRAME_CLASS:
            return win.pid
        for child in self.u.children(win.hwnd):
            if child.class_name == CORE_WINDOW_CLASS and child.pid != win.pid:
                return child.pid
        return None  # suspended / not yet running packaged app

    def _scan(self) -> list[_Group]:
        own = self._own_pids()
        session = self.k.session_of(self.k.current_pid())
        groups: dict[str, _Group] = {}
        infos: dict[int, ProcInfo | None] = {}
        for win in self.u.windows():
            if not win.visible or win.owned or not win.titled or win.cloaked:
                continue
            pid = self._real_pid(win)
            if pid is None or pid in own:
                continue
            if pid not in infos:
                infos[pid] = self.k.process_info(pid)
            info = infos[pid]
            if info is None or (session is not None and info.session != session):
                continue
            if self._protected(info.image_path):
                continue
            group = groups.setdefault(
                _norm_path(info.image_path), _Group(info.image_path, None, {}, [])
            )
            group.procs.setdefault(pid, info)
            group.hwnds.append(win.hwnd)
            group.aumid = group.aumid or info.aumid
        return list(groups.values())

    def _description(self, path: str) -> str | None:
        if path not in self._descriptions:
            try:
                self._descriptions[path] = self.v.description(path)
            except Exception:
                log.debug("version info failed", exc_info=True)
                self._descriptions[path] = None
        return self._descriptions[path]

    def _to_app(self, group: _Group) -> RunningApp:
        first = group.first()
        return RunningApp(
            key=_make_key(first),
            name=self._description(group.image_path) or _stem(group.image_path),
            exe_path=group.image_path,
            app_id=group.aumid,
            pids=tuple(sorted(group.procs)),
            windows=len(group.hwnds),
        )

    def list_running(self) -> Sequence[RunningApp]:
        return tuple(self._to_app(g) for g in self._scan())

    # --- identity ---------------------------------------------------------------------------

    def identity_matches(self, app: RunningApp, entry: AppEntry) -> bool:
        kind = classify_app_id(entry.app_id)
        if kind == "aumid":
            return bool(app.app_id) and app.app_id.casefold() == entry.app_id.casefold()  # type: ignore[union-attr]
        if not app.exe_path:
            return False
        if kind == "abs_path":
            return _norm_path(app.exe_path) == _norm_path(entry.app_id)
        if kind == "guid_path":
            folder = self.sh.known_folder(entry.app_id[:38])
            if not folder:
                return False
            resolved = ntpath.join(folder, entry.app_id[39:])
            return _norm_path(app.exe_path) == _norm_path(resolved)
        return False  # "simple" ids, URLs, CLSIDs have no process identity

    # --- acting -----------------------------------------------------------------------------

    def _find(self, app: RunningApp) -> _Group | None:
        """Re-find the app now; None if it is gone or the key process changed (pid reuse)."""
        parsed = _parse_key(app.key)
        if parsed is None:
            return None
        path, pid, creation = parsed
        if self._protected(path):
            raise ProtectedApp("this app is protected and is never closed")
        for group in self._scan():
            if _norm_path(group.image_path) != _norm_path(path):
                continue
            info = group.procs.get(pid)
            if info is not None and info.creation == creation:
                return group
        return None

    def _alive(self, procs: Iterable[ProcInfo]) -> list[ProcInfo]:
        alive = []
        for p in procs:
            now = self.k.process_info(p.pid)
            if now is not None and now.creation == p.creation:
                alive.append(now)
        return alive

    def _owned_visible(self, pids: set[int]) -> int:
        return sum(1 for w in self.u.windows() if w.pid in pids and w.owned and w.visible)

    def _visible_windows(self, pids: set[int], hwnds: Iterable[int]) -> list[WindowInfo]:
        frames = set(hwnds)  # UWP frames belong to ApplicationFrameHost, not to the app pid
        return [
            w
            for w in self.u.windows()
            if (w.pid in pids or w.hwnd in frames)
            and w.visible
            and not w.owned
            and w.titled
            and not w.cloaked
        ]

    def close(self, app: RunningApp, *, wait_s: float) -> CloseOutcome:
        group = self._find(app)
        if group is None:
            return CloseOutcome.NOT_RUNNING
        pids = set(group.procs)
        procs = list(group.procs.values())
        baseline = self._owned_visible(pids)
        for hwnd in group.hwnds:
            self.u.post_close(hwnd)
        deadline = self._clock() + max(wait_s, 0.0)
        while True:
            if not self._alive(procs):
                return CloseOutcome.CLOSED
            if self._visible_windows(pids, group.hwnds) and self._owned_visible(pids) > baseline:
                return CloseOutcome.PROMPTED
            if self._clock() >= deadline:
                break
            self._sleep(POLL_INTERVAL_S)
        open_windows = self._visible_windows(pids, group.hwnds)
        if not open_windows:
            return CloseOutcome.HIDDEN
        # One hang probe per close, after the wait (each probe may block up to a second). Hung
        # or just slow, a window that ignored WM_CLOSE for the whole wait is "not responding".
        hung = any(self.u.is_hung(w.hwnd) for w in open_windows)
        log.debug("window still open after close (hung=%s)", hung)
        return CloseOutcome.NOT_RESPONDING

    def terminate(self, app: RunningApp) -> None:
        group = self._find(app)
        if group is None:
            raise ToolError("the app is no longer running or has changed")
        table = self._table()
        own = self._own_pids()
        targets: dict[int, ProcInfo] = {pid: info for pid, info in group.procs.items()}
        # Children: same image, same session, started no earlier than their parent (a reused
        # parent pid would otherwise adopt unrelated processes), never our own processes.
        frontier = dict(targets)
        while frontier:
            nxt: dict[int, ProcInfo] = {}
            for pid, parent in table.items():
                parent_info = frontier.get(parent)
                if parent_info is None or pid in targets or pid in own:
                    continue
                info = self.k.process_info(pid)
                if (
                    info is not None
                    and _norm_path(info.image_path) == _norm_path(group.image_path)
                    and info.session == parent_info.session
                    and info.creation >= parent_info.creation
                ):
                    nxt[pid] = info
            targets.update(nxt)
            frontier = nxt
        killed = 0
        for pid in sorted(targets):
            info = targets[pid]
            if self._protected(info.image_path):
                raise ProtectedApp("this app is protected and is never closed")
            if self.k.terminate(pid, info.creation):
                killed += 1
        if killed == 0:
            raise ToolError("could not terminate the app")
