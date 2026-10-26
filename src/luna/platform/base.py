"""OS-neutral platform protocols. Windows implementations live in ``luna.platform.windows``.

Failures of ``launch`` / ``open_url`` / hotkey and autostart operations are reported as
``luna.core.errors.ToolError`` so tools can turn them into ``ToolResult(ok=False)``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
