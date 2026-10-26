"""Make the process safe to run without a console (``pythonw``).

Under ``pythonw.exe`` ``sys.stdout``/``sys.stderr`` are ``None``: ``print()`` is silently dropped
but ``stream.write`` (tqdm, library warnings) raises. :func:`install` replaces missing streams
with devnull writers, routes unhandled exceptions to ``logging`` (and so through the secret
redaction filters of the handlers set up by :mod:`luna.logs.setup`) and enables ``faulthandler``
writing to ``crash.log``.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import IO

from luna.logs.setup import default_log_dir

CRASH_FILENAME = "crash.log"

_logger = logging.getLogger("luna.crash")
_installed = False
_crash_file: IO[str] | None = None  # kept open for the process lifetime (faulthandler needs it)


def _devnull() -> IO[str]:
    return open(os.devnull, "w", encoding="utf-8")


def _log_uncaught(
    exc_type: type[BaseException],
    exc: BaseException | None,
    tb: TracebackType | None,
    *,
    where: str = "main thread",
) -> None:
    with contextlib.suppress(Exception):  # a hook must never raise
        if exc is None:
            _logger.critical("Unhandled %s in %s", exc_type.__name__, where)
        else:
            _logger.critical("Unhandled exception in %s", where, exc_info=(exc_type, exc, tb))


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _log_uncaught(exc_type, exc, tb)


def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "unknown thread"
    _log_uncaught(args.exc_type, args.exc_value, args.exc_traceback, where=f"thread {name}")


def install(log_dir: Path | None = None) -> None:
    """Install windowless-safe stdio, exception hooks and the crash log. Idempotent."""
    global _installed, _crash_file
    if _installed:
        return
    if sys.stdout is None:
        sys.stdout = _devnull()
    if sys.stderr is None:
        sys.stderr = _devnull()
    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook

    directory = Path(log_dir) if log_dir is not None else default_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _crash_file = open(directory / CRASH_FILENAME, "a", encoding="utf-8")  # noqa: SIM115
    faulthandler.enable(file=_crash_file, all_threads=True)
    _installed = True
