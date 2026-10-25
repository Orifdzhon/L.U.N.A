"""Windows global hotkey (``RegisterHotKey``) running in its own message-loop thread.

Design notes (ADR 0007):

* A hotkey registered with ``hWnd=NULL`` is bound to the **registering thread**, which must
  also pump its message queue. So one dedicated thread registers, reports the result back to
  ``start()`` synchronously, then loops on ``GetMessageW`` and unregisters on exit.
* ``stop()`` posts ``WM_QUIT`` to that thread (``PostThreadMessageW``) and joins it.
* Only ``RegisterHotKey`` is used: no low-level keyboard hook is ever installed (hooks are
  antivirus-unfriendly and not needed). The price is that only key *presses* are reported,
  hence toggle-style push-to-talk. ``MOD_NOREPEAT`` suppresses auto-repeat while held.
* The message thread must stay responsive, so ``on_press`` runs on a separate small worker
  thread fed by a bounded queue (extra presses are dropped when the callback is slow).
  An exception in ``on_press`` is logged and never ends the loop.
* ``ctypes`` / ``user32`` are touched only inside :class:`_User32` and only lazily, so this
  module imports on any OS; unit tests replace ``_User32`` with a fake.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from luna.core.errors import ToolError

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_HOTKEY_ID = 1  # ids are per-thread, so a constant is enough
_START_TIMEOUT_S = 5.0
_STOP_TIMEOUT_S = 2.0
_MAX_PENDING = 8

_MODIFIERS: dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

_NAMED_KEYS: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}


@dataclass(frozen=True)
class ParsedHotkey:
    modifiers: int  # MOD_* mask, without MOD_NOREPEAT
    vk: int  # virtual-key code


def _key_code(name: str) -> int | None:
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if len(name) == 1 and "a" <= name <= "z":
        return ord(name.upper())
    if len(name) == 1 and "0" <= name <= "9":
        return ord(name)
    if name.startswith("f") and name[1:].isdigit() and not name[1:].startswith("0"):
        number = int(name[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return None


def parse_hotkey(text: str) -> ParsedHotkey:
    """Parse ``"ctrl+alt+space"`` into a modifier mask and a virtual-key code.

    Requires at least one modifier and exactly one non-modifier key, written last.
    Raises ``ToolError`` for anything else.
    """
    if not isinstance(text, str) or not text.strip():
        raise ToolError("invalid hotkey: empty (expected e.g. 'ctrl+alt+space')")
    parts = [p.strip().lower() for p in text.split("+")]
    if any(not p for p in parts):
        raise ToolError(f"invalid hotkey {text!r}: empty part (expected e.g. 'ctrl+alt+space')")
    *mod_names, key_name = parts
    modifiers = 0
    for name in mod_names:
        flag = _MODIFIERS.get(name)
        if flag is None:
            raise ToolError(
                f"invalid hotkey {text!r}: {name!r} is not a modifier "
                f"(use {', '.join(_MODIFIERS)}; the key goes last)"
            )
        if modifiers & flag:
            raise ToolError(f"invalid hotkey {text!r}: duplicate modifier {name!r}")
        modifiers |= flag
    if key_name in _MODIFIERS:
        raise ToolError(f"invalid hotkey {text!r}: no key after the modifiers")
    if not modifiers:
        raise ToolError(f"invalid hotkey {text!r}: at least one modifier is required")
    vk = _key_code(key_name)
    if vk is None:
        raise ToolError(
            f"invalid hotkey {text!r}: unknown key {key_name!r} "
            "(letters, digits, f1-f24, space, enter, esc, tab, arrows, ...)"
        )
    return ParsedHotkey(modifiers, vk)


class _User32:
    """Thin ctypes wrapper over the few Win32 calls needed. Replaced by a fake in tests."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _Msg(ctypes.Structure):
            _fields_ = (
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            )

        self._ctypes = ctypes
        self._msg_type = _Msg
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u = self._user32
        u.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        u.RegisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        u.UnregisterHotKey.restype = wintypes.BOOL
        u.GetMessageW.argtypes = (ctypes.POINTER(_Msg), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        u.GetMessageW.restype = wintypes.BOOL
        u.PeekMessageW.argtypes = (
            ctypes.POINTER(_Msg),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        u.PeekMessageW.restype = wintypes.BOOL
        u.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        u.PostThreadMessageW.restype = wintypes.BOOL
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def current_thread_id(self) -> int:
        return int(self._kernel32.GetCurrentThreadId())

    def create_message_queue(self) -> None:
        """Force creation of the calling thread's message queue (``PeekMessage``)."""
        msg = self._msg_type()
        self._user32.PeekMessageW(self._ctypes.byref(msg), None, 0, 0, 0)  # PM_NOREMOVE

    def register_hotkey(self, hotkey_id: int, modifiers: int, vk: int) -> bool:
        return bool(self._user32.RegisterHotKey(None, hotkey_id, modifiers, vk))

    def unregister_hotkey(self, hotkey_id: int) -> None:
        self._user32.UnregisterHotKey(None, hotkey_id)

    def last_error(self) -> int:
        return int(self._ctypes.get_last_error())

    def get_message(self) -> tuple[int, int, int]:
        """Block for the next message; returns ``(result, message, wParam)``.

        ``result`` is 0 on ``WM_QUIT`` and -1 on error."""
        msg = self._msg_type()
        result = int(self._user32.GetMessageW(self._ctypes.byref(msg), None, 0, 0))
        return result, int(msg.message), int(msg.wParam or 0)

    def post_quit(self, thread_id: int) -> None:
        self._user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)


class WindowsHotkeyListener:
    """``HotkeyListener`` implementation backed by ``RegisterHotKey``."""

    def __init__(self, user32_factory: Callable[[], Any] | None = None) -> None:
        self._factory = user32_factory or _User32
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._thread_id = 0
        self._u32: Any = None
        self._pending: queue.Queue[bool | None] = queue.Queue(_MAX_PENDING)

    # -- public API ---------------------------------------------------------------------

    def start(self, hotkey: str, on_press: Callable[[], None]) -> None:
        parsed = parse_hotkey(hotkey)
        with self._lock:
            if self._thread is not None:
                raise ToolError("hotkey listener is already running; call stop() first")
            try:
                u32 = self._factory()
            except Exception as exc:
                raise ToolError(f"global hotkeys are unavailable: {exc}") from exc
            self._u32 = u32
            self._pending = queue.Queue(_MAX_PENDING)
            ready = threading.Event()
            outcome: dict[str, Any] = {}
            thread = threading.Thread(
                target=self._run,
                args=(u32, parsed, ready, outcome),
                name="luna-hotkey",
                daemon=True,
            )
            worker = threading.Thread(
                target=self._work,
                args=(self._pending, on_press),
                name="luna-hotkey-cb",
                daemon=True,
            )
            thread.start()
            if not ready.wait(_START_TIMEOUT_S):
                outcome["abandon"] = True
                thread.join(_STOP_TIMEOUT_S)
                raise ToolError(f"timed out registering hotkey {hotkey!r}")
            error = outcome.get("error")
            if error is not None:
                thread.join(_STOP_TIMEOUT_S)
                if error == ERROR_HOTKEY_ALREADY_REGISTERED:
                    raise ToolError(
                        f"hotkey {hotkey!r} is already taken by another program; "
                        "choose another combination in [voice] hotkey"
                    )
                raise ToolError(f"could not register hotkey {hotkey!r} (Win32 error {error})")
            self._thread_id = outcome["thread_id"]
            self._thread = thread
            self._worker = worker
            worker.start()
            log.info("global hotkey %s registered", hotkey)

    def stop(self) -> None:
        with self._lock:
            thread, worker = self._thread, self._worker
            if thread is None:
                return
            self._thread = self._worker = None
            self._u32.post_quit(self._thread_id)
            thread.join(_STOP_TIMEOUT_S)
            if thread.is_alive():
                log.warning("hotkey thread did not stop within %.1fs", _STOP_TIMEOUT_S)
            self._pending.put(None)
            if worker is not None and worker is not threading.current_thread():
                worker.join(_STOP_TIMEOUT_S)

    # -- threads ------------------------------------------------------------------------

    def _run(self, u32: Any, parsed: ParsedHotkey, ready: threading.Event, outcome: dict) -> None:
        registered = False
        try:
            u32.create_message_queue()
            registered = u32.register_hotkey(_HOTKEY_ID, parsed.modifiers | MOD_NOREPEAT, parsed.vk)
            if not registered:
                outcome["error"] = u32.last_error()
                return
            outcome["thread_id"] = u32.current_thread_id()
        except Exception as exc:
            log.exception("hotkey thread setup failed")
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            return
        finally:
            ready.set()
        try:
            if outcome.get("abandon"):
                return
            while True:
                result, message, wparam = u32.get_message()
                if result <= 0:
                    break
                if message == WM_HOTKEY and wparam == _HOTKEY_ID:
                    try:
                        self._pending.put_nowait(True)
                    except queue.Full:
                        log.debug("hotkey press dropped: callback is busy")
        except Exception:
            log.exception("hotkey message loop failed")
        finally:
            if registered:
                u32.unregister_hotkey(_HOTKEY_ID)

    @staticmethod
    def _work(pending: queue.Queue, on_press: Callable[[], None]) -> None:
        while True:
            item = pending.get()
            if item is None:
                return
            try:
                on_press()
            except Exception:
                log.exception("hotkey callback raised")
