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


@runtime_checkable
class Autostart(Protocol):
    def is_enabled(self) -> bool: ...

    def enable(self, command: str) -> None:
        """Register L.U.N.A.'s own launch command to run at login.

        ``command`` must be built by the application from its own entry point, never from
        user or LLM text; implementations quote it and refuse an empty command.
        """
        ...

    def disable(self) -> None: ...
