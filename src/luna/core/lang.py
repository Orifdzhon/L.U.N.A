# ruff: noqa: RUF001
"""Language detection shared by the router, the speaker and others (pure, no I/O)."""

from __future__ import annotations

import re

SUPPORTED_LANGS = ("ru", "en")


def detect_lang(text: str, hint: str | None = None) -> str:
    """Cyrillic text -> "ru", other letters -> "en"; no letters: the hint, else "ru"."""
    if re.search(r"[а-яё]", text, re.IGNORECASE):
        return "ru"
    if re.search(r"[a-z]", text, re.IGNORECASE):
        return "en"
    return hint if hint in SUPPORTED_LANGS else "ru"
