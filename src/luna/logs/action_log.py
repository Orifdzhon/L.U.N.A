"""Append-only JSONL action log (spec F9): one JSON object per tool call.

Rotation: monthly, by file name. ``ActionLog(path)`` takes the base path
(``.../actions.jsonl``) and writes to ``.../actions-YYYY-MM.jsonl``; a new month simply starts
a new file, old files are never rewritten or deleted.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from luna.logs.redact import redact_value

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True, kw_only=True)
class ActionRecord:
    """One executed (or refused) tool call. Field order is the JSON key order."""

    ts: str = ""  # filled by ActionLog.write from its clock when empty
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
    """Thread-safe writer of :class:`ActionRecord` lines (UTF-8, ``ensure_ascii=False``).

    Failure policy: an audit-log problem must never fail or hide an action that already ran.
    ``write`` catches ``OSError``, reports it once through the ``luna.logs.action_log`` logger
    (tool name and request id only, never the record contents) and returns ``False``.

    Readers must skip lines that do not parse: a crash mid-write can leave a truncated line,
    and the next record is then appended to it.
    """

    def __init__(self, path: Path, clock: Callable[[], datetime] = _now) -> None:
        self._base = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        # On failure the directory is created lazily on first write, where errors are reported.
        with contextlib.suppress(OSError):
            self._base.parent.mkdir(parents=True, exist_ok=True)

    def _path_for(self, moment: datetime) -> Path:
        stamp = moment.strftime("%Y-%m")
        return self._base.with_name(f"{self._base.stem}-{stamp}{self._base.suffix}")

    def current_path(self) -> Path:
        """File the next record goes to (depends on the current month)."""
        return self._path_for(self._clock())

    def write(self, record: ActionRecord) -> bool:
        """Append ``record`` as exactly one line; secrets are masked in every field.

        Returns ``True`` on success, ``False`` if the line could not be written.
        """
        moment = self._clock()  # one clock read: the timestamp and the file month agree
        values = {f.name: getattr(record, f.name) for f in fields(record)}
        values["ts"] = record.ts or moment.isoformat(timespec="milliseconds")
        data = redact_value(values)
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            path = self._path_for(moment)
            try:
                try:
                    self._append(path, line)
                except FileNotFoundError:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._append(path, line)
            except OSError as exc:
                logger.warning(
                    "action log write failed (tool=%s request_id=%s): %s",
                    record.tool,
                    record.request_id,
                    type(exc).__name__,
                )
                return False
        return True

    @staticmethod
    def _append(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
