"""The command that starts L.U.N.A. itself (autostart shortcut). No Qt import."""

from __future__ import annotations

import sys
from pathlib import Path

from luna.platform.base import LaunchCommand

WINDOWLESS_PYTHON = "pythonw.exe"
FROZEN_EXE = "luna.exe"
FIXED_ARGS = ("-m", "luna", "--tray", "--minimized")


def own_launch_command(
    executable: Path,
    config: Path | None,
    cwd: Path,
    frozen: bool | None = None,
) -> LaunchCommand:
    """``pythonw -m luna --tray --minimized [--config <abs>]`` in ``cwd``.

    Frozen build (``sys.frozen``): ``<install dir>\luna.exe --tray --minimized [--config <abs>]``
    with the install dir as working directory, also when the running exe is ``luna-cli.exe``.

    ``executable`` is ``sys.executable`` (the interpreter of the current venv); ``pythonw`` is
    its sibling. Built by the application only: never takes window, transcript or LLM text.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    tail = ["--config", str(config.resolve())] if config is not None else []
    if frozen:
        return LaunchCommand(
            executable=str(executable.with_name(FROZEN_EXE)),
            args=(*FIXED_ARGS[2:], *tail),
            working_dir=str(executable.parent),
        )
    args = [*FIXED_ARGS, *tail]
    return LaunchCommand(
        executable=str(executable.with_name(WINDOWLESS_PYTHON)),
        args=tuple(args),
        working_dir=str(cwd),
    )
