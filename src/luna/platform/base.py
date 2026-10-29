"""OS-neutral platform protocols. Windows implementations live in ``luna.platform.windows``.

Failures of ``launch`` / ``open_url`` / hotkey and autostart operations are reported as
``luna.core.errors.ToolError`` so tools can turn them into ``ToolResult(ok=False)``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from luna.core.errors import ToolError


@dataclass(frozen=True)
class AppEntry:
    name: str  # display name, possibly localized
    app_id: str  # OS-specific launch id (AUMID on Windows)
    aliases: tuple[str, ...] = ()


@runtime_checkable
class AppCatalog(Protocol):
    def list_apps(self) -> Sequence[AppEntry]:
        """Return an immutable snapshot of the cached catalog (never a live, mutable list)."""
        ...

    def refresh(self) -> None:
        """Re-scan installed apps. The new catalog replaces the old one atomically, so a
        concurrent ``list_apps()`` sees either the old or the new snapshot, never a mix."""
        ...


@runtime_checkable
class AppLauncher(Protocol):
    def launch(self, app: AppEntry) -> None:
        """Launch an entry that came from an ``AppCatalog``; raises ``ToolError`` on failure."""
        ...


@runtime_checkable
class BrowserOpener(Protocol):
    def open_url(self, url: str) -> None:
        """Open ``url`` in the default browser.

        Raises ``ValueError`` for any scheme other than http/https (ARCHITECTURE section 9)
        and ``ToolError`` if the browser cannot be opened.
        """
        ...


@runtime_checkable
class HotkeyListener(Protocol):
    def start(self, hotkey: str, on_press: Callable[[], None]) -> None:
        """Register a global hotkey written as ``+``-joined lowercase names, e.g.
        ``"ctrl+alt+space"``. ``on_press`` is called on a background (hotkey) thread, not
        the UI thread. Raises ``ToolError`` if the combination is invalid or already taken."""
        ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class LaunchCommand:
    """How to start L.U.N.A. itself. Built by the application, never from user or LLM text."""

    executable: str
    args: tuple[str, ...] = ()
    working_dir: str | None = None


@runtime_checkable
class Autostart(Protocol):
    def is_enabled(self) -> bool:
        """Whether L.U.N.A. is registered to run at login. Never raises."""
        ...

    def enable(self, command: LaunchCommand) -> None:
        """Register (or update) the login entry; raises ``ToolError`` on failure."""
        ...

    def disable(self) -> None:
        """Remove the login entry. Idempotent; raises ``ToolError`` on failure."""
        ...


@runtime_checkable
class SingleInstance(Protocol):
    def acquire(self) -> bool:
        """Try to become the only running instance; False if another one already runs."""
        ...

    def release(self) -> None:
        """Give the claim up. Idempotent."""
        ...


class CloseOutcome(enum.Enum):
    CLOSED = "closed"  # every process of the app exited
    PROMPTED = "prompted"  # the app asks the user something (e.g. "save changes?")
    HIDDEN = "hidden"  # windows are gone but the process lives on (tray apps)
    NOT_RESPONDING = "not_responding"  # a window is hung or did not close in time
    NOT_RUNNING = "not_running"  # already gone


class ProtectedApp(ToolError):
    """The target is a protected (system / security / own) process and is never touched."""


@dataclass(frozen=True)
class RunningApp:
    """A running app as listed by ``RunningApps``. Window titles are never part of it."""

    key: str  # opaque identity "<image path>|<pid>|<creation time>"; never shown
    name: str  # display name: exe FileDescription, else exe stem
    exe_path: str | None
    app_id: str | None  # AUMID of a packaged app, else None
    pids: tuple[int, ...]
    windows: int  # visible top-level windows (count only)


@runtime_checkable
class RunningApps(Protocol):
    def list_running(self) -> Sequence[RunningApp]:
        """Snapshot of running user apps. Protected processes never appear."""
        ...

    def close(self, app: RunningApp, *, wait_s: float) -> CloseOutcome:
        """Ask the app to close like its window's X button, wait up to ``wait_s`` seconds.
        Raises ``ToolError`` on failure and ``ProtectedApp`` for protected targets."""
        ...

    def terminate(self, app: RunningApp) -> None:
        """Kill the app's processes. Raises ``ToolError`` if the app is gone or changed (pid
        reuse) and ``ProtectedApp`` for protected targets."""
        ...

    def identity_matches(self, app: RunningApp, entry: AppEntry) -> bool:
        """Whether ``app`` is the running instance of the catalog ``entry`` (AUMID / exe path)."""
        ...
