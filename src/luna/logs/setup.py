"""Logging setup: console (CLI mode) and rotating file handlers, both with secret redaction."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

from luna.logs.redact import RedactionFilter, register_secrets

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_FILENAME = "luna.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 4  # luna.log + 4 backups = 5 files of up to 2 MB


class _LunaStreamHandler(logging.StreamHandler):
    """Marker subclass so repeated setup calls replace only our own handlers."""


class _LunaFileHandler(RotatingFileHandler):
    """Marker subclass, see :class:`_LunaStreamHandler`."""


def default_log_dir(env: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\LUNA\\logs`` (falls back to ``~/AppData/Local`` if unset)."""
    env = os.environ if env is None else env
    base = env.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "LUNA" / "logs"


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    *,
    console: bool = True,
    secrets: Iterable[str] = (),
) -> Path:
    """Configure the root logger and return the path of the log file.

    ``log_dir`` defaults to :func:`default_log_dir`. ``secrets`` are registered for exact-value
    masking (see :func:`luna.logs.redact.register_secret`). Calling it again replaces the
    handlers installed earlier.
    """
    register_secrets(secrets)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _LunaStreamHandler | _LunaFileHandler):
            root.removeHandler(existing)
            existing.close()

    directory = Path(log_dir) if log_dir is not None else default_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / LOG_FILENAME

    handlers: list[logging.Handler] = [
        _LunaFileHandler(
            log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=True
        )
    ]
    if console:
        handlers.append(_LunaStreamHandler())
    formatter = logging.Formatter(LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactionFilter())
        root.addHandler(handler)
    root.setLevel(level.upper())
    return log_path
