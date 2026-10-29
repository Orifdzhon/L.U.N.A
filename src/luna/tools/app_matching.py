"""Resolve a spoken/typed app name to an ``AppEntry`` of the catalog (F4, ADR 0008).

A wrong launch is worse than no launch, so the matcher is deliberately strict and works on
whole tokens, never on substrings:

1. Normalize (casefold, yo-to-ye folding, punctuation) and strip only *leading/trailing*
   politeness and open-verbs ("открой", "please", ...).
2. Queries with other verbs ("закрой", "delete", ...) or joining several apps ("и", "and", ",")
   are not ours: ``UNSUPPORTED``.
3. Aliases ("хром" -> "Google Chrome") are resolved through the same strict matcher, so they
   only ever reach apps that exist in the catalog.
4. EVERY query token must match a distinct token of the app name: equal, equal after
   Cyrillic->Latin transliteration, or (tokens of 4+ chars) ``rapidfuzz.fuzz.ratio`` >=
   ``threshold`` on the whole token. Extra name tokens may remain ("epic games" -> "Epic Games
   Launcher"). Uninstallers are never candidates; docs/help entries lose to real apps.
5. Among candidates within ``margin`` points of the best mean token score, a unique exact
   full-name match wins; otherwise one candidate is a match and several are ambiguous.

The matcher only ever returns entries from the catalog list it is given.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from rapidfuzz import fuzz

from luna.core.errors import ConfigError
from luna.platform.base import AppEntry

DEFAULT_THRESHOLD = 85
DEFAULT_MARGIN = 5
MAX_NAME_CHARS = 100
MIN_QUERY_CHARS = 2
MIN_FUZZY_TOKEN_CHARS = 4
MAX_CANDIDATES = 5

# Dropped only at the start or the end of the query ("открой пожалуйста телеграм").
_FILLER_WORDS = frozenset(
    [
        "открой",
        "открыть",
        "запусти",
        "запустить",
        "запускай",
        "включи",
        "включить",
        "стартуй",
        "пожалуйста",
        "плиз",
        "мне",
        "приложение",
        "программу",
        "программа",
        "прогу",
        "app",
        "application",
        "program",
        "open",
        "launch",
        "start",
        "run",
        "please",
        "the",
        "my",
        "луна",
        "luna",
    ]
)
# Anywhere in the query: another intent than "open an app" -> not our job.
_OTHER_VERBS = frozenset(
    [
        "закрой",
        "закрыть",
        "закрывай",
        "удали",
        "удалить",
        "удаляй",
        "деинсталлируй",
        "снеси",
        "убей",
        "останови",
        "выключи",
        "выключить",
        "перезапусти",
        "перезагрузи",
        "обнови",
        "обновить",
        "установи",
        "установить",
        "скачай",
        "close",
        "quit",
        "kill",
        "stop",
        "delete",
        "remove",
        "uninstall",
        "install",
        "update",
        "download",
        "restart",
        "reboot",
        "shutdown",
    ]
)
# verbs="close": these are fillers at the edges instead of "another intent".
_CLOSE_FILLERS = frozenset(
    [
        "закрой",
        "закрыть",
        "закрывай",
        "выключи",
        "выключить",
        "выйди",
        "из",
        "close",
        "quit",
        "exit",
    ]
)
_CONJUNCTIONS = frozenset(["и", "также", "плюс", "and", "or", "plus", "then", "потом", "затем"])
# One generic word is not an app name ("музыка", "browser"); only an exact full name matches.
_GENERIC_WORDS = frozenset(
    [
        "музыка",
        "музыку",
        "браузер",
        "игра",
        "игры",
        "чат",
        "почта",
        "почту",
        "плеер",
        "видео",
        "фото",
        "редактор",
        "music",
        "browser",
        "game",
        "games",
        "chat",
        "mail",
        "player",
        "video",
        "photo",
        "photos",
        "editor",
        "app",
        "apps",
    ]
)
_DOC_TOKENS = frozenset(
    [
        "manual",
        "manuals",
        "docs",
        "doc",
        "documentation",
        "help",
        "readme",
        "справка",
        "руководство",
        "документация",
    ]
)
_UNINSTALL_MARKERS = ("uninstall", "uninst", "удалить", "удаление", "деинсталл", "remove")

_FORBIDDEN_CHARS = frozenset('\\/:*?"<>|%$`;&=~^{}[]@#!')
_PUNCT_RE = re.compile(r"[^\w\s+.\-]", re.UNICODE)

_CYRILLIC = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
_LATIN = [
    "a",
    "b",
    "v",
    "g",
    "d",
    "e",
    "zh",
    "z",
    "i",
    "i",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "r",
    "s",
    "t",
    "u",
    "f",
    "h",
    "c",
    "ch",
    "sh",
    "sh",
    "_",
    "y",
    "_",
    "e",
    "u",
    "ya",
]
_TRANSLIT = {c: ("" if p == "_" else p) for c, p in zip(_CYRILLIC, _LATIN, strict=True)}


class MatchStatus(Enum):
    MATCH = "match"
    AMBIGUOUS = "ambiguous"
    NONE = "none"
    UNSUPPORTED = "unsupported"  # not a plain "open <app>" request


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    entry: AppEntry | None = None
    candidates: tuple[AppEntry, ...] = ()


_NONE = MatchResult(MatchStatus.NONE)
_UNSUPPORTED = MatchResult(MatchStatus.UNSUPPORTED)


def normalize_name(text: str) -> str:
    """Lowercase, yo-to-ye folding, punctuation to spaces, whitespace collapsed."""
    return " ".join(_PUNCT_RE.sub(" ", text.casefold().replace("ё", "е")).split())  # noqa: RUF001


def _strip_fillers(tokens: list[str], fillers: frozenset[str] = _FILLER_WORDS) -> list[str]:
    start, end = 0, len(tokens)
    while start < end - 1 and tokens[start] in fillers:
        start += 1
    while end > start + 1 and tokens[end - 1] in fillers:
        end -= 1
    return tokens[start:end]


def normalize_query(text: str) -> str:
    """``normalize_name`` plus removal of leading/trailing open-verbs and politeness."""
    return " ".join(_strip_fillers(normalize_name(text).split()))


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


@lru_cache(maxsize=4096)
def _name_variants(entry: AppEntry) -> tuple[tuple[str, ...], ...]:
    """Token lists of the display name and of each entry alias (cached per entry)."""
    variants = (normalize_name(x).split() for x in (entry.name, *entry.aliases))
    return tuple(tuple(v) for v in variants if v)


def _is_uninstaller(entry: AppEntry) -> bool:
    text = f"{entry.name} {entry.app_id}".casefold()
    return any(marker in text for marker in _UNINSTALL_MARKERS)


def _candidates_pool(apps: Sequence[AppEntry]) -> list[AppEntry]:
    """Drop uninstallers and duplicates (same app_id, or same display name: lowest id wins)."""
    by_name: dict[str, AppEntry] = {}
    for e in sorted(apps, key=lambda a: a.app_id):
        if _is_uninstaller(e) or not _name_variants(e):
            continue
        by_name.setdefault(normalize_name(e.name), e)
    return list(by_name.values())


def _token_score(q: str, n: str, threshold: int) -> float:
    if q == n:
        return 100.0
    tq, tn = transliterate(q), transliterate(n)
    if tq == tn:
        return 100.0
    if len(q) < MIN_FUZZY_TOKEN_CHARS or len(n) < MIN_FUZZY_TOKEN_CHARS:
        return 0.0
    score = max(fuzz.ratio(q, n), fuzz.ratio(tq, tn))
    return score if score >= threshold else 0.0


def _entry_fit(query: list[str], entry: AppEntry, threshold: int) -> tuple[float, int] | None:
    """(mean token score, unmatched name tokens) if EVERY query token is explained, else None."""
    best: tuple[float, int] | None = None
    for name in _name_variants(entry):
        free = list(name)
        scores: list[float] = []
        for q in query:
            cand = max(
                ((_token_score(q, n, threshold), i) for i, n in enumerate(free)), default=None
            )
            if cand is None or cand[0] <= 0:
                break
            scores.append(cand[0])
            free.pop(cand[1])
        else:
            fit = (sum(scores) / len(scores), len(free))
            if best is None or (-fit[0], fit[1]) < (-best[0], best[1]):
                best = fit
    return best


def _strict_match(
    query: list[str], pool: Sequence[AppEntry], threshold: int, margin: int
) -> MatchResult:
    fits = [(f, e) for e in pool if (f := _entry_fit(query, e, threshold)) is not None]
    if not fits:
        return _NONE
    phrase = set(query)
    if not phrase & _DOC_TOKENS:  # docs/help entries lose to real apps
        real = [(f, e) for f, e in fits if not any(_DOC_TOKENS & set(v) for v in _name_variants(e))]
        fits = real or fits
    top = max(f[0] for f, _ in fits)
    close = [(f, e) for f, e in fits if top - f[0] < margin or f[0] == top]
    # An exact full-name match (no unmatched name tokens) wins over longer names; otherwise only
    # a single candidate is a match: "studio" must not pick LM Studio because it is shorter.
    exact = [e for f, e in close if f[1] == 0]
    chosen = exact if len(exact) == 1 else [e for _, e in close]
    if len(chosen) == 1:
        return MatchResult(MatchStatus.MATCH, chosen[0], (chosen[0],))
    return MatchResult(MatchStatus.AMBIGUOUS, None, tuple(chosen[:MAX_CANDIDATES]))


def match_app(
    name: str,
    apps: Sequence[AppEntry],
    aliases: Mapping[str, tuple[str, ...]] | None = None,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    margin: int = DEFAULT_MARGIN,
    verbs: Literal["open", "close"] = "open",
) -> MatchResult:
    """Pick the catalog entry for ``name``. Never returns an entry that is not in ``apps``.

    ``verbs="close"`` treats "закрой/close/quit/выйди из ..." as edge fillers (for
    ``close_app``) instead of rejecting them as another intent.
    """
    fillers = _FILLER_WORDS | _CLOSE_FILLERS if verbs == "close" else _FILLER_WORDS
    other_verbs = _OTHER_VERBS - _CLOSE_FILLERS if verbs == "close" else _OTHER_VERBS
    if not isinstance(name, str) or len(name) > MAX_NAME_CHARS:
        return _NONE
    if any(ch in _FORBIDDEN_CHARS or not ch.isprintable() for ch in name):
        return _NONE  # paths, command lines, shell metacharacters
    tokens = _strip_fillers(normalize_name(name).split(), fillers)
    query = " ".join(tokens)
    if len(query) < MIN_QUERY_CHARS:
        return _NONE
    pool = _candidates_pool(apps)
    if not pool:
        return _NONE

    full_name = any(query == normalize_name(e.name) for e in pool)
    if not full_name:
        if "," in name or any(t in other_verbs or t in _CONJUNCTIONS for t in tokens):
            return _UNSUPPORTED
        if all(t in _GENERIC_WORDS for t in tokens):
            return _NONE

    # alias -> its targets go through the same strict matcher (first target that resolves)
    for target in (aliases or {}).get(query, ()):
        result = _strict_match(normalize_name(target).split(), pool, threshold, margin)
        if result.status is not MatchStatus.NONE:
            return result

    return _strict_match(tokens, pool, threshold, margin)
