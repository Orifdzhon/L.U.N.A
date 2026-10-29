"""Fakes and helpers for the file search tests (kept out of ``fakes.py`` on purpose)."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from luna.core.errors import ProviderUnavailable, ToolError
from luna.files.base import FileHit, FileQuery, RootDir, SearchOutcome


class FakeFileOpener:
    """Records what would be opened / revealed; nothing is ever launched."""

    def __init__(self, error: ToolError | None = None) -> None:
        self.opened: list[Path] = []
        self.revealed: list[Path] = []
        self.error = error

    def open(self, path: Path) -> None:
        if self.error:
            raise self.error
        self.opened.append(path)

    def reveal(self, path: Path) -> None:
        if self.error:
            raise self.error
        self.revealed.append(path)


class FakeFileIndex:
    def __init__(
        self,
        hits: Sequence[FileHit] = (),
        *,
        available: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.hits = list(hits)
        self._available = available
        self.error = error
        self.calls: list[FileQuery] = []

    def available(self) -> bool:
        return self._available

    def query(self, q, roots, *, limit, timeout_s):
        self.calls.append(q)
        if self.error is not None:
            raise self.error
        return list(self.hits)[:limit]


class StubSearch:
    """Stands in for ``FileSearch``: returns a canned outcome."""

    def __init__(
        self, hits: Sequence[FileHit], roots: Sequence[RootDir], *, timed_out: bool = False
    ) -> None:
        self.outcome = SearchOutcome(tuple(hits), "walk", timed_out, 10, 0.01, tuple(roots))
        self.queries: list[FileQuery] = []

    def search(self, q, cancel=None):
        self.queries.append(q)
        return self.outcome


class StepClock:
    """Every call advances time by ``step`` seconds."""

    def __init__(self, step: float = 0.0, start: float = 0.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class LogCapture(logging.Handler):
    """Collects the messages of the ``luna`` loggers whatever other tests did to logging."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []
        self._logger = logging.getLogger("luna")
        self._saved = (self._logger.level, self._logger.disabled)

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def __enter__(self) -> LogCapture:
        self._logger.addHandler(self)
        self._logger.setLevel(logging.DEBUG)
        self._logger.disabled = False
        return self

    def __exit__(self, *exc: object) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._saved[0])
        self._logger.disabled = self._saved[1]

    @property
    def text(self) -> str:
        return " | ".join(self.messages)


def make_files(base: Path, names: Sequence[str], content: bytes = b"x") -> list[Path]:
    """Create files (and parent folders) under ``base``; names ending in ``/`` are folders."""
    out = []
    for name in names:
        path = base / name
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        out.append(path)
    return out


def hit_for(path: Path, label: str = "Documents", *, is_dir: bool | None = None) -> FileHit:
    """A FileHit with the real size / mtime of ``path`` (as the walker would report)."""
    st = os.stat(path)
    directory = path.is_dir() if is_dir is None else is_dir
    return FileHit(
        path,
        directory,
        None if directory else st.st_size,
        datetime.fromtimestamp(st.st_mtime, UTC),
        label,
    )


def fake_hit(
    path: str,
    *,
    is_dir: bool = False,
    size: int | None = 10,
    day: int = 1,
    label: str = "Documents",
) -> FileHit:
    return FileHit(Path(path), is_dir, size, datetime(2026, 1, day, tzinfo=UTC), label)


__all__ = [
    "FakeFileIndex",
    "FakeFileOpener",
    "LogCapture",
    "ProviderUnavailable",
    "StepClock",
    "StubSearch",
    "fake_hit",
    "hit_for",
    "make_files",
]
