"""Rank file hits against a query (pure).

Score = rapidfuzz ``token_set_ratio`` of the best-matching variant of every token against the
file name (extension dropped, separators as spaces), +5 when every token is a whole word, +3
when the user's own spelling matched (not an alias or transliteration).
Hits that miss the kind filter or any token are dropped; ties go to the more recent file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from rapidfuzz import fuzz

from luna.files.base import FileHit, FileQuery
from luna.files.query import matches, normalize_text

DEFAULT_MIN_SCORE = 70
_SEP_RE = re.compile(r"[\s_.\-]+")
_CAMEL_RE = re.compile(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])")  # noqa: RUF001 (Cyrillic ranges)


def _suffix(hit: FileHit) -> str:
    return "" if hit.is_dir else os.path.splitext(hit.path.name)[1].casefold()


def score(q: FileQuery, hit: FileHit) -> float:
    """About 0..108; 0 when the hit does not satisfy the query."""
    full = normalize_text(hit.path.name)
    if not matches(q, full, is_dir=hit.is_dir, suffix=_suffix(hit)):
        return 0.0
    stem = normalize_text(_CAMEL_RE.sub(" ", hit.path.name if hit.is_dir else hit.path.stem))
    words = _SEP_RE.sub(" ", stem).strip()
    present = [max((v for v in variants if v in full), key=len) for variants in q.tokens]
    value = float(fuzz.token_set_ratio(" ".join(present), words))
    whole = set(words.split())
    if all(p in whole for p in present):
        value += 5
    if all(p == variants[0] for p, variants in zip(present, q.tokens, strict=True)):
        value += 3  # the user's own spelling beats a transliteration or an alias
    return value


def rank(
    hits: Iterable[FileHit], q: FileQuery, *, limit: int, min_score: float = DEFAULT_MIN_SCORE
) -> list[FileHit]:
    """Best ``limit`` distinct hits with a score of at least ``min_score``."""
    seen: set[str] = set()
    scored: list[tuple[float, float, FileHit]] = []
    for hit in hits:
        key = os.path.normcase(str(hit.path))
        if key in seen:
            continue
        seen.add(key)
        value = score(q, hit)
        if value >= min_score:
            when = hit.modified.timestamp() if hit.modified else 0.0
            scored.append((value, when, hit))
    scored.sort(key=lambda t: (-t[0], -t[1], len(t[2].path.name), str(t[2].path)))
    return [hit for _, _, hit in scored[: max(0, limit)]]
