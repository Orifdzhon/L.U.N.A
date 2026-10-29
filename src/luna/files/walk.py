"""Bounded breadth-first folder walk with ``os.scandir`` (ADR 0011, Feature B).

Names only: files are never opened and nothing is stat'ed beyond what ``scandir`` returns (on
Windows that data comes with the directory listing, so OneDrive placeholders are not
hydrated). Skipped entirely: names on the skip list, hidden/system entries, symlinks and
junctions (loops), and folders that are themselves another search root (walked in their own
turn). The walk stops at the deadline, after ``max_entries`` entries, or when ``cancel`` is
set; the result then says it did not cover everything.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from luna.core.clock import Clock
from luna.files.base import FileHit, FileQuery, RootDir
from luna.files.query import matches, normalize_text

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
_CHECK_EVERY = 256  # entries between deadline checks inside one folder
MAX_HITS = 1000  # candidates kept before ranking

DEFAULT_SKIP = frozenset(
    name.casefold()
    for name in (
        "Windows",
        "Program Files",
        "Program Files (x86)",
        "ProgramData",
        "$Recycle.Bin",
        "System Volume Information",
        "Recovery",
        "$WinREAgent",
        "$Windows.~BT",
        "$Windows.~WS",
        "MSOCache",
        "PerfLogs",
        "AppData",
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        ".uv-cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        ".gradle",
        ".m2",
        ".nuget",
        ".cargo",
        ".rustup",
    )
)


@dataclass
class WalkResult:
    hits: list[FileHit] = field(default_factory=list)
    timed_out: bool = False  # some folders were not checked
    cancelled: bool = False
    scanned: int = 0


def _hit(entry: os.DirEntry[str], is_dir: bool, label: str) -> FileHit:
    size: int | None = None
    modified: datetime | None = None
    try:
        st = entry.stat(follow_symlinks=False)
        size = None if is_dir else st.st_size
        modified = datetime.fromtimestamp(st.st_mtime, UTC)
    except (OSError, ValueError, OverflowError):
        pass
    return FileHit(Path(entry.path), is_dir, size, modified, label)


def _is_link(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def _hidden(entry: os.DirEntry[str]) -> bool:
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def walk(
    roots: Sequence[RootDir],
    q: FileQuery,
    *,
    deadline: float,
    max_entries: int,
    skip: frozenset[str] = DEFAULT_SKIP,
    clock: Clock,
    cancel: threading.Event | None = None,
    max_hits: int = MAX_HITS,
) -> WalkResult:
    """Walk ``roots`` in order; each gets an equal share of the time left (unused rolls over)."""
    result = WalkResult()
    root_keys = {os.path.normcase(str(r.path)) for r in roots}
    for position, root in enumerate(roots):
        if cancel is not None and cancel.is_set():
            result.cancelled = result.timed_out = True
            return result
        now = clock()
        if now >= deadline or result.scanned >= max_entries:
            result.timed_out = True
            return result
        share = now + (deadline - now) / (len(roots) - position)
        _walk_root(root, q, share, max_entries, skip, root_keys, clock, cancel, max_hits, result)
        if result.cancelled or len(result.hits) >= max_hits:
            break
    return result


def _walk_root(
    root: RootDir,
    q: FileQuery,
    deadline: float,
    max_entries: int,
    skip: frozenset[str],
    root_keys: set[str],
    clock: Clock,
    cancel: threading.Event | None,
    max_hits: int,
    result: WalkResult,
) -> None:
    queue: deque[str] = deque([str(root.path)])
    while queue:
        if clock() >= deadline or (cancel is not None and cancel.is_set()):
            result.timed_out = True
            result.cancelled = cancel is not None and cancel.is_set()
            return
        directory = queue.popleft()
        try:
            iterator = os.scandir(directory)
        except OSError:
            continue
        with iterator:
            for count, entry in enumerate(iterator):
                result.scanned += 1
                if result.scanned > max_entries:
                    result.timed_out = True
                    return
                if count % _CHECK_EVERY == _CHECK_EVERY - 1 and clock() >= deadline:
                    result.timed_out = True
                    return
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                name = normalize_text(entry.name)
                if is_dir and (
                    name in skip or _is_link(entry) or os.path.normcase(entry.path) in root_keys
                ):
                    continue
                if _hidden(entry):
                    continue
                suffix = "" if is_dir else os.path.splitext(name)[1]
                if matches(q, name, is_dir=is_dir, suffix=suffix):
                    result.hits.append(_hit(entry, is_dir, root.label))
                    if len(result.hits) >= max_hits:
                        return
                if is_dir:
                    queue.append(entry.path)
