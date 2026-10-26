"""The command that starts L.U.N.A. itself (autostart shortcut). No Qt import."""

from __future__ import annotations

from pathlib import Path

from luna.platform.base import LaunchCommand

WINDOWLESS_PYTHON = "pythonw.exe"
FIXED_ARGS = ("-m", "luna", "--tray", "--minimized")


def own_launch_command(executable: Path, config: Path | None, cwd: Path) -> LaunchCommand:
    """``pythonw -m luna --tray --minimized [--config <abs>]`` in ``cwd``.

    ``executable`` is ``sys.executable`` (the interpreter of the current venv); ``pythonw`` is
    its sibling. Built by the application only: never takes window, transcript or LLM text.
    """
    args = list(FIXED_ARGS)
    if config is not None:
        args += ["--config", str(config.resolve())]
    return LaunchCommand(
        executable=str(executable.with_name(WINDOWLESS_PYTHON)),
        args=tuple(args),
        working_dir=str(cwd),
    )
