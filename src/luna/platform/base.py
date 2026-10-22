"""OS-neutral platform protocols. Windows implementations live in ``luna.platform.windows``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AppEntry:
    name: str  # display name, possibly localized
    app_id: str  # OS-specific launch id (AUMID on Windows)
    aliases: tuple[str, ...] = ()


@runtime_checkable
class AppCatalog(Protocol):
    def list_apps(self) -> list[AppEntry]: ...  # cached; refresh() re-scans

    def refresh(self) -> None: ...


@runtime_checkable
class AppLauncher(Protocol):
    def launch(self, app: AppEntry) -> None: ...


@runtime_checkable
class BrowserOpener(Protocol):
    def open_url(self, url: str) -> None: ...  # http/https only


@runtime_checkable
class HotkeyListener(Protocol):
    def start(self, hotkey: str, on_press: Callable[[], None]) -> None: ...

    def stop(self) -> None: ...


@runtime_checkable
class Autostart(Protocol):
    def is_enabled(self) -> bool: ...

    def enable(self, command: str) -> None: ...

    def disable(self) -> None: ...
