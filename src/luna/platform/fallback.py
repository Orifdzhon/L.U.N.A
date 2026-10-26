"""Fallbacks for platforms without a native implementation."""

from __future__ import annotations

from luna.core.errors import ToolError
from luna.platform.base import LaunchCommand


class NoAutostart:
    """Autostart is not supported here: never enabled, ``enable`` refuses."""

    def is_enabled(self) -> bool:
        return False

    def enable(self, command: LaunchCommand) -> None:
        raise ToolError("autostart is not supported on this platform")

    def disable(self) -> None:
        return None


class ProcessLocalInstance:
    """No cross-process guard: always succeeds."""

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None
