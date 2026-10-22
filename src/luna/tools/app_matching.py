"""Resolve a spoken/typed app name to an ``AppEntry`` of the catalog (F4, ADR 0008).

Pipeline: normalize (casefold, yo-to-ye folding, strip verbs/fillers/punctuation) -> alias lookup
-> exact name -> fuzzy ``rapidfuzz.WRatio`` on the original text and on a Cyrillic->Latin
transliteration of both sides. A match needs ``score >= threshold`` and a lead of at least
``margin`` over the runner-up; near-equal candidates are reported as ambiguous.

The matcher only ever returns entries from the catalog list it is given, so nothing outside the
catalog can be launched. Raw paths and command lines are rejected before matching.
"""

# ruff: noqa: RUF001
from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from typing import Any

from rapidfuzz import fuzz

from luna.core.errors import ConfigError
from luna.platform.base import AppEntry

DEFAULT_THRESHOLD = 85
DEFAULT_MARGIN = 5
MAX_NAME_CHARS = 100
MIN_QUERY_CHARS = 2
MAX_CANDIDATES = 5

# Whole words dropped from the query ("открой пожалуйста телеграм" -> "телеграм").
_STOP_WORDS = frozenset(
    {
        "открой", "открыть", "откроешь", "запусти", "запустить", "запускай", "включи",
        "включить", "стартуй", "пожалуйста", "плиз", "мне", "приложение", "программу",
        "программа", "прогу", "app", "application", "program", "open", "launch", "start",
        "run", "please", "the", "my", "луна", "luna",
    }
)  # fmt: skip
_FORBIDDEN_CHARS = frozenset('\\/:*?"<>|%$`;&=~^{}[]@#!')
_PUNCT_RE = re.compile(r"[^\w\s+.\-]", re.UNICODE)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "u", "я": "ya",
}  # fmt: skip


class MatchStatus(Enum):
    MATCH = "match"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    entry: AppEntry | None = None
    candidates: tuple[AppEntry, ...] = ()


_NONE = MatchResult(MatchStatus.NONE)


def normalize_name(text: str) -> str:
    """Lowercase, yo-to-ye folding, punctuation to spaces, whitespace collapsed (verbs kept)."""
    return " ".join(_PUNCT_RE.sub(" ", text.casefold().replace("ё", "е")).split())


def normalize_query(text: str) -> str:
    """``normalize_name`` plus removal of command verbs and filler words."""
    return " ".join(w for w in normalize_name(text).split() if w not in _STOP_WORDS)


def transliterate(text: str) -> str:
    """Cyrillic -> Latin (lossy, for comparing STT spellings like "телеграм" with "Telegram")."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


def _clean_aliases(table: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for key, value in table.items():
        targets = [value] if isinstance(value, str) else value
        if (
            not isinstance(key, str)
            or not normalize_query(key)
            or not isinstance(targets, list | tuple)
            or not targets
            or not all(isinstance(t, str) and normalize_name(t) for t in targets)
        ):
            raise ConfigError(f"invalid app alias {key!r}: expected an app name or a list of names")
        out[normalize_query(key)] = tuple(targets)
    return out


def default_app_aliases() -> dict[str, tuple[str, ...]]:
    """Shipped aliases from ``luna/data/app_aliases.toml`` (normalized keys)."""
    raw = (resources.files("luna") / "data" / "app_aliases.toml").read_text(encoding="utf-8")
    return _clean_aliases(tomllib.loads(raw))


def merge_aliases(user: Mapping[str, Any] | None = None) -> dict[str, tuple[str, ...]]:
    """Shipped aliases with the user's overrides on top."""
    aliases = default_app_aliases()
    aliases.update(_clean_aliases(user or {}))
    return aliases


def _unique(entries: Sequence[AppEntry]) -> list[AppEntry]:
    seen: set[str] = set()
    out: list[AppEntry] = []
    for e in entries:
        if e.app_id not in seen:
            seen.add(e.app_id)
            out.append(e)
    return out


def _names(entry: AppEntry) -> list[str]:
    return [n for n in (normalize_name(x) for x in (entry.name, *entry.aliases)) if n]


def _verdict(found: Sequence[AppEntry]) -> MatchResult:
    if not found:
        return _NONE
    if len(found) == 1:
        return MatchResult(MatchStatus.MATCH, found[0], (found[0],))
    return MatchResult(MatchStatus.AMBIGUOUS, None, tuple(found[:MAX_CANDIDATES]))


def _score(query: str, query_lat: str, entry: AppEntry) -> float:
    best = 0.0
    for name in _names(entry):
        best = max(best, fuzz.WRatio(query, name), fuzz.WRatio(query_lat, transliterate(name)))
    return best


def match_app(
    name: str,
    apps: Sequence[AppEntry],
    aliases: Mapping[str, tuple[str, ...]] | None = None,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    margin: int = DEFAULT_MARGIN,
) -> MatchResult:
    """Pick the catalog entry for ``name``. Never returns an entry that is not in ``apps``."""
    if not isinstance(name, str) or len(name) > MAX_NAME_CHARS:
        return _NONE
    if any(ch in _FORBIDDEN_CHARS or not ch.isprintable() for ch in name):
        return _NONE  # paths, command lines, shell metacharacters
    query = normalize_query(name)
    if len(query) < MIN_QUERY_CHARS or not apps:
        return _NONE
    entries = _unique(apps)

    # 1. alias -> catalog entry whose name equals one of the alias targets (priority order)
    for target in (aliases or {}).get(query, ()):
        wanted = normalize_name(target)
        if found := [e for e in entries if wanted in _names(e)]:
            return _verdict(found)

    # 2. exact (normalized) name or entry alias
    if found := [e for e in entries if query in _names(e)]:
        return _verdict(found)

    # 3. fuzzy
    query_lat = transliterate(query)
    scored = sorted(
        ((_score(query, query_lat, e), e) for e in entries), key=lambda p: p[0], reverse=True
    )
    top = scored[0][0]
    if top < threshold:
        return _NONE
    return _verdict([e for s, e in scored if s >= threshold and (s == top or top - s < margin)])
