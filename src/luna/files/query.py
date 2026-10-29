# ruff: noqa: RUF001, RUF002
"""Turn a spoken file request into a :class:`FileQuery` (pure; no I/O except the word tables).

Steps: NFC, casefold, ``ё -> е``; stop words dropped; kind words ("пдф", "таблица", "папку")
become an extension filter / directories-only; every remaining word becomes a token with
variants: the word, its 5-letter stem, its Latin transliteration, month names and aliases.
At most :data:`MAX_TOKENS` tokens, each ``[\\w .-]{1,40}``; characters that could form a path
or a wildcard are rejected.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

from luna.core.errors import ConfigError
from luna.files.base import FileQuery
from luna.tools.app_matching import transliterate

MAX_TOKENS = 4
MAX_TOKEN_CHARS = 40
MAX_QUERY_CHARS = 120
STEM_CHARS = 5  # words longer than this are cut for the index / substring match

_FORBIDDEN_RE = re.compile(r'[\\/:*<>|"%\[\]{}~]')
_SPLIT_RE = re.compile(r"[\s_,;!?()«»'`+=#&@^$]+")  # separators; "." and "-" stay inside words
_TOKEN_RE = re.compile(r"[\w.-]{1,40}")


class QueryError(ValueError):
    """The request cannot be turned into a file search."""


def normalize_text(text: str) -> str:
    """NFC, casefold, ``ё -> е``. Used for queries and for file names alike."""
    return unicodedata.normalize("NFC", text).casefold().replace("ё", "е")


@dataclass(frozen=True)
class KindTable:
    stop_words: frozenset[str]
    folder_words: frozenset[str]
    kind_words: Mapping[str, str]  # word -> kind name
    kind_extensions: Mapping[str, frozenset[str]]  # kind name -> extensions
    months: Mapping[str, tuple[str, ...]]  # normalized month form -> variants
    aliases: Mapping[str, tuple[str, ...]]  # shipped aliases
    open_extensions: frozenset[str]
    never_open: frozenset[str]

    def can_open(self, suffix: str) -> bool:
        """D7: only documents, media and archives are opened; the rest is revealed."""
        suffix = suffix.casefold()
        return suffix in self.open_extensions and suffix not in self.never_open


def load_kinds() -> KindTable:
    """The shipped word tables (``luna/data/file_kinds.toml``)."""
    raw = (resources.files("luna") / "data" / "file_kinds.toml").read_text(encoding="utf-8")
    return _build(tomllib.loads(raw))


def _build(data: dict[str, Any]) -> KindTable:
    kind_words: dict[str, str] = {}
    kind_ext: dict[str, frozenset[str]] = {}
    for name, entry in data.get("kinds", {}).items():
        kind_ext[name] = frozenset(e.casefold() for e in entry["extensions"])
        for word in entry["words"]:
            kind_words[normalize_text(word)] = name
    months: dict[str, tuple[str, ...]] = {}
    for forms in data.get("months", {}).values():
        norm = tuple(normalize_text(f) for f in forms)
        for form in norm:
            months[form] = norm
    opened = data.get("open", {})
    return KindTable(
        stop_words=frozenset(normalize_text(w) for w in data.get("stop_words", [])),
        folder_words=frozenset(normalize_text(w) for w in data.get("folder_words", [])),
        kind_words=kind_words,
        kind_extensions=kind_ext,
        months=months,
        aliases=clean_aliases(data.get("aliases", {})),
        open_extensions=frozenset(e.casefold() for e in opened.get("extensions", [])),
        never_open=frozenset(e.casefold() for e in opened.get("never_open", [])),
    )


def clean_aliases(table: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Validate an alias table (word -> one or more spellings); keys are normalized."""
    out: dict[str, tuple[str, ...]] = {}
    for key, value in table.items():
        targets = [value] if isinstance(value, str) else value
        if (
            not isinstance(key, str)
            or not normalize_text(key).strip()
            or not isinstance(targets, list | tuple)
            or not targets
            or not all(isinstance(t, str) and normalize_text(t).strip() for t in targets)
        ):
            raise ConfigError(f"invalid file alias {key!r}: expected a word or a list of words")
        out[normalize_text(key).strip()] = tuple(normalize_text(t).strip() for t in targets)
    return out


_CYR = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
_LAT = ["a","b","v","g","d","e","zh","z","i","y","k","l","m","n","o","p","r","s","t","u","f",
        "kh","ts","ch","sh","shch","","y","","e","yu","ya"]  # fmt: skip
_COMMON = dict(zip(_CYR, _LAT, strict=True))


def _latin(text: str) -> str:
    """Common Russian-to-Latin spelling ("резюме" -> "rezyume"), for names typed in Latin."""
    return "".join(_COMMON.get(ch, ch) for ch in text)


def _variants(
    word: str, kinds: KindTable, aliases: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    out: list[str] = []

    def add(value: str) -> None:
        if value and value not in out:
            out.append(value)

    add(word)
    if not word.isdigit():
        stem = word[:STEM_CHARS]
        add(stem)
        for convert in (_latin, transliterate):
            add(convert(word))
            add(convert(stem))
        for form in kinds.months.get(word, ()):
            add(form)
        for key, targets in aliases.items():
            if key[:STEM_CHARS] == stem:
                for alias in targets:
                    add(alias)
    return tuple(out)


def parse_query(
    text: str,
    kinds: KindTable,
    aliases: Mapping[str, tuple[str, ...]] | None = None,
    *,
    kind: str | None = None,
) -> FileQuery:
    """Parse ``text`` (and an optional explicit ``kind`` such as "pdf" or "folder").

    Raises :class:`QueryError` for forbidden characters, no usable words or an unknown kind.
    """
    if not isinstance(text, str):
        raise QueryError("the query must be text")
    if len(text) > MAX_QUERY_CHARS:
        raise QueryError("the query is too long")
    if _FORBIDDEN_RE.search(text) or any(unicodedata.category(c) in ("Cc", "Cf") for c in text):
        raise QueryError("the query contains characters that cannot be part of a file name")
    merged = dict(kinds.aliases)
    merged.update(aliases or {})

    extensions: set[str] = set()
    dirs_only = False
    if kind and kind not in ("any", ""):
        if kind == "folder":
            dirs_only = True
        elif kind in kinds.kind_extensions:
            extensions |= kinds.kind_extensions[kind]
        else:
            raise QueryError(f"unknown kind {kind!r}")

    tokens: list[tuple[str, ...]] = []
    for raw in _SPLIT_RE.split(normalize_text(text)):
        word = raw.strip(".-")
        if not word or word in kinds.stop_words:
            continue
        if word in kinds.folder_words:
            dirs_only = True
        elif word in kinds.kind_words:
            extensions |= kinds.kind_extensions[kinds.kind_words[word]]
        elif _TOKEN_RE.fullmatch(word) is None or len(word) > MAX_TOKEN_CHARS:
            raise QueryError("a word of the query is too long or has invalid characters")
        else:
            tokens.append(_variants(word, kinds, merged))
    if not tokens:
        raise QueryError("say what to look for: the query has no name words")
    return FileQuery(
        tokens=tuple(tokens[:MAX_TOKENS]),
        extensions=frozenset(extensions) if extensions and not dirs_only else None,
        dirs_only=dirs_only,
    )


def matches(q: FileQuery, name: str, *, is_dir: bool, suffix: str = "") -> bool:
    """Does the (already normalized) file ``name`` satisfy the kind filter and every token?"""
    if q.dirs_only and not is_dir:
        return False
    if q.extensions is not None and (is_dir or suffix not in q.extensions):
        return False
    return all(any(v in name for v in variants) for variants in q.tokens)
