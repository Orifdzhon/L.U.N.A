"""Pick the platform objects for the running OS."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from luna.config.settings import FilesSettings
from luna.files.base import FileOpener, RootDir
from luna.files.roots import resolve_roots
from luna.platform.base import Autostart, SingleInstance
from luna.platform.fallback import NoAutostart, ProcessLocalInstance


def create_autostart() -> Autostart:
    if sys.platform == "win32":
        from luna.platform.windows.autostart import StartupFolderAutostart

        return StartupFolderAutostart()
    return NoAutostart()


def create_single_instance() -> SingleInstance:
    if sys.platform == "win32":
        from luna.platform.windows.single_instance import NamedMutexInstance

        return NamedMutexInstance()
    return ProcessLocalInstance()


def create_file_opener() -> FileOpener | None:
    """The Explorer opener on Windows; None elsewhere (file search is Windows-only for now)."""
    if sys.platform == "win32":
        from luna.platform.windows.files import ShellFileOpener

        return ShellFileOpener()
    return None


def create_roots_provider(conf: FilesSettings) -> Callable[[], Sequence[RootDir]]:
    """A callable returning the current search roots (re-evaluated for every search)."""
    known: Callable[[], Sequence[tuple[str, Path]]]
    drives: Callable[[], Sequence[Path]]
    if sys.platform == "win32":
        from luna.platform.windows.files import fixed_drives, known_folders

        known, drives = known_folders, fixed_drives
    else:

        def known() -> Sequence[tuple[str, Path]]:
            return [("Home", Path.home())]

        def drives() -> Sequence[Path]:
            return []

    return lambda: resolve_roots(conf, known_folders=known, drives=drives)
