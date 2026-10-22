"""Append-only JSONL action log (spec F9): one JSON object per tool call.

Rotation: monthly, by file name. ``ActionLog(path)`` takes the base path
(``.../actions.jsonl``) and writes to ``.../actions-YYYY-MM.jsonl``; a new month simply starts
a new file, old files are never rewritten or deleted.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from luna.logs.redact import redact_value


def _now() -> datetime:
    return datetime.now().astimezone()


def _timestamp() -> str:
    return _now().isoformat(timespec="milliseconds")


@dataclass(frozen=True, kw_only=True)
class ActionRecord:
    """One executed (or refused) tool call. Field order is the JSON key order."""

    ts: str = field(default_factory=_timestamp)
    request_id: str
    source: str  # "text" | "voice"
    text: str  # the user's request
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    safety: str  # SafetyLevel value, kept as plain text to avoid depending on tools
    confirmed: bool | None = None  # None: no confirmation was needed
    ok: bool
    summary: str = ""
    duration_ms: int = 0
    provider: str | None = None


class ActionLog:
    """Thread-safe writer of :class:`ActionRecord` lines (UTF-8, ``ensure_ascii=False``)."""

    def __init__(self, path: Path, clock: Callable[[], datetime] = _now) -> None:
        self._base = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def current_path(self) -> Path:
        """File the next record goes to (depends on the current month)."""
        stamp = self._clock().strftime("%Y-%m")
        return self._base.with_name(f"{self._base.stem}-{stamp}{self._base.suffix}")

    def write(self, record: ActionRecord) -> None:
        """Append ``record`` as exactly one line; secrets are masked in every field."""
        data = redact_value({f.name: getattr(record, f.name) for f in fields(record)})
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            path = self.current_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
