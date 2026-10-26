"""Single-instance guard backed by a Windows named mutex (ADR 0009).

``Local\\`` scopes the mutex to the login session. ``ctypes`` is touched only inside
:class:`_Kernel32` and lazily, so the module imports on any OS; tests replace it with a fake.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183
DEFAULT_MUTEX_NAME = "Local\\LUNA-instance"


class _Kernel32:
    """Thin ctypes wrapper over the Win32 calls needed. Replaced by a fake in tests."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        self._k = k

    def create_mutex(self, name: str) -> tuple[int | None, int]:
        """Return ``(handle or None, last_error)``."""
        self._ctypes.set_last_error(0)
        handle = self._k.CreateMutexW(None, False, name)
        return (int(handle) if handle else None), int(self._ctypes.get_last_error())

    def close_handle(self, handle: int) -> None:
        self._k.CloseHandle(handle)


class NamedMutexInstance:
    """``SingleInstance`` implementation using ``CreateMutexW``."""

    def __init__(
        self,
        name: str = DEFAULT_MUTEX_NAME,
        kernel32_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._name = name
        self._factory = kernel32_factory or _Kernel32
        self._k: Any = None
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if self._k is None:
            self._k = self._factory()
        handle, error = self._k.create_mutex(self._name)
        if handle is None:
            # Cannot create the mutex at all: do not block the app over a guard failure.
            log.warning("CreateMutexW failed (error %s); continuing without a guard", error)
            return True
        if error == ERROR_ALREADY_EXISTS:
            self._k.close_handle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._k.close_handle(handle)
