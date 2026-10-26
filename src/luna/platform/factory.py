"""Pick the platform objects for the running OS."""

from __future__ import annotations

import sys

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
