"""Secret redaction for logs: key-shaped patterns plus exact registered values.

``register_secret`` keeps the registry for the whole process. Secret values are never logged
or exposed by this module. :class:`RedactionFilter` must be attached to *handlers* (logger
filters do not run for records propagated from child loggers).
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from collections.abc import Iterable
from typing import Any

MASK = "***"
MIN_SECRET_LENGTH = 4  # shorter values would mask ordinary words

_KEY_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google
    re.compile(r"gsk_[0-9A-Za-z_\-]{10,}"),  # Groq
    re.compile(r"sk-[0-9A-Za-z_\-]{10,}"),  # OpenAI-style
)
_BEARER = re.compile(r"(Bearer\s+)[0-9A-Za-z._~+/=\-]{8,}", re.IGNORECASE)

_lock = threading.Lock()
_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask this exact value everywhere from now on (blank or very short values are ignored)."""
    value = (value or "").strip()
    if len(value) >= MIN_SECRET_LENGTH:
        with _lock:
            _secrets.add(value)


def register_secrets(values: Iterable[str]) -> None:
    for value in values:
        register_secret(value)


def clear_secrets() -> None:
    """Forget all registered values (for tests)."""
    with _lock:
        _secrets.clear()


def redact_text(text: str) -> str:
    """Return ``text`` with registered secrets and key-shaped tokens masked."""
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)  # longest first
    for secret in known:
        text = text.replace(secret, MASK)
    for pattern in _KEY_PATTERNS:
        text = pattern.sub(MASK, text)
    return _BEARER.sub(rf"\1{MASK}", text)


def redact_value(value: Any) -> Any:
    """Redact strings inside (nested) dicts/lists; other scalars pass through unchanged.

    Objects that are not JSON-native are converted with ``str()`` first, so the result is
    always safe to serialize and contains no secrets.
    """
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {redact_text(str(k)): redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(v) for v in value]
    return redact_text(str(value))


class RedactionFilter(logging.Filter):
    """Mask secrets in the message, its arguments, the traceback and the stack info."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format/args: keep the line, drop the arguments
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True
