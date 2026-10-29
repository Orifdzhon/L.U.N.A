"""``FileSearch``: OS index first (optional), then a bounded walk of the uncovered roots, then rank.

Runs on the caller's (turn) thread under one total time budget. Only counts, durations and the
backend are logged, never names or paths.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace

from luna.config.settings import FilesSettings
from luna.core.clock import Clock
from luna.files.base import FileHit, FileIndex, FileQuery, RootDir, SearchOutcome
from luna.files.query import normalize_text
from luna.files.rank import rank
from luna.files.walk import DEFAULT_SKIP, path_blocked, walk

log = logging.getLogger(__name__)

INDEX_LIMIT = 200
INDEX_TIMEOUT_S = 2.0


def root_of(path: str, roots: Sequence[RootDir]) -> RootDir | None:
    """Innermost root containing ``path`` (case-insensitive on Windows)."""
    key = os.path.normcase(path)
    best: RootDir | None = None
    for root in roots:
        base = os.path.normcase(str(root.path)).rstrip("\\/")
        if (key == base or key.startswith(base + os.sep)) and (
            best is None or len(str(root.path)) > len(str(best.path))
        ):
            best = root
    return best


class FileSearch:
    def __init__(
        self,
        roots: Callable[[], Sequence[RootDir]],
        index: FileIndex | None,
        conf: FilesSettings,
        clock: Clock = time.monotonic,
    ) -> None:
        self._roots = roots
        self._index = index
        self._conf = conf
        self._clock = clock
        self._skip = DEFAULT_SKIP | frozenset(normalize_text(n) for n in conf.exclude)

    def search(self, q: FileQuery, cancel: threading.Event | None = None) -> SearchOutcome:
        start = self._clock()
        deadline = start + self._conf.time_budget_s
        roots = sorted(self._roots(), key=lambda r: not r.user)  # user folders first (stable)
        hits: list[FileHit] = []
        covered: set[str] = set()
        backend = "walk"

        indexed = self._from_index(q, roots, deadline - start)
        if indexed is not None:
            backend = "index"
            for hit in indexed:
                root = root_of(str(hit.path), roots)
                if root is not None:
                    hits.append(replace(hit, root_label=root.label))
                    covered.add(os.path.normcase(str(root.path)))

        todo = [r for r in roots if os.path.normcase(str(r.path)) not in covered]
        timed_out = False
        scanned = 0
        if todo and not (cancel is not None and cancel.is_set()):
            walked = walk(
                todo,
                q,
                deadline=deadline,
                max_entries=self._conf.max_entries,
                skip=self._skip,
                clock=self._clock,
                cancel=cancel,
            )
            hits.extend(walked.hits)
            timed_out, scanned = walked.timed_out, walked.scanned
            backend = "index+walk" if backend == "index" else "walk"
        everything = rank(hits, q, limit=len(hits))
        ranked = everything[: self._conf.max_results]
        elapsed = self._clock() - start
        log.info(
            "file search: %d result(s) from %d candidate(s), backend=%s, scanned=%d, "
            "timed_out=%s, %d ms",
            len(ranked),
            len(hits),
            backend,
            scanned,
            timed_out,
            round(elapsed * 1000),
        )
        return SearchOutcome(
            tuple(ranked), backend, timed_out, scanned, elapsed, tuple(roots), len(everything)
        )

    def _from_index(
        self, q: FileQuery, roots: Sequence[RootDir], budget: float
    ) -> list[FileHit] | None:
        """Index hits inside the roots and outside skipped folders; None if unavailable."""
        index = self._index
        if index is None or not roots:
            return None
        try:
            if not index.available():
                return None
            found = index.query(
                q,
                [r.path for r in roots],
                limit=INDEX_LIMIT,
                timeout_s=min(INDEX_TIMEOUT_S, budget),
            )
        except Exception as exc:
            log.info("file index unavailable (%s); walking instead", type(exc).__name__)
            return None
        kept: list[FileHit] = []
        for hit in found:
            root = root_of(str(hit.path), roots)
            if root is None:
                continue
            parts = hit.path.parts[len(root.path.parts) :]
            if any(normalize_text(p) in self._skip for p in parts[:-1]):
                continue
            if path_blocked(hit.path, root.path):
                continue
            kept.append(hit)
        return kept
