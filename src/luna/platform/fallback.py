"""Fallbacks for platforms without a native implementation."""

from __future__ import annotations

from collections.abc import Sequence

from luna.core.errors import ToolError
from luna.platform.base import AppEntry, CloseOutcome, LaunchCommand, RunningApp


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


class NoRunningApps:
    """Closing apps is not supported here: nothing is listed, close/terminate refuse."""

    supported = False

    def list_running(self) -> Sequence[RunningApp]:
        return ()

    def close(self, app: RunningApp, *, wait_s: float) -> CloseOutcome:
        raise ToolError("closing apps is not supported on this platform")

    def terminate(self, app: RunningApp) -> None:
        raise ToolError("closing apps is not supported on this platform")

    def identity_matches(self, app: RunningApp, entry: AppEntry) -> bool:
        return False
