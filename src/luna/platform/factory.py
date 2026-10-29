"""Pick the platform objects for the running OS."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from luna.platform.base import Autostart, RunningApps, SingleInstance
from luna.platform.fallback import NoAutostart, NoRunningApps, ProcessLocalInstance


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


def create_running_apps(protected_extra: Sequence[str] = ()) -> RunningApps:
    if sys.platform == "win32":
        from luna.platform.windows.processes import WindowsRunningApps

        return WindowsRunningApps(protected_extra=protected_extra)
    return NoRunningApps()
