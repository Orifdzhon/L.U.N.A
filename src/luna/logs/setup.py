"""Logging setup. Console only for now; rotation and redaction come later."""

import logging
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class _LunaHandler(logging.StreamHandler):
    """Marker subclass so repeated setup calls replace only our own handler."""


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure the root logger with a console handler.

    ``log_dir`` is accepted for API stability; file logging is not implemented yet.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _LunaHandler):
            root.removeHandler(existing)
    handler = _LunaHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(level.upper())
