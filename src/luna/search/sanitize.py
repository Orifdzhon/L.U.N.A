"""Text hygiene for untrusted web content."""

from __future__ import annotations

import re
import unicodedata

# Cc control, Cf format (zero-width, bidi controls, tag characters), Cs surrogate,
# Co private use, Cn unassigned. Newlines and tabs are handled separately.
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
# NEL, vertical tab, form feed, line separator (U+2028), paragraph separator (U+2029).
_NEWLINES = frozenset(["\n", "\r", "\x0b", "\x0c", chr(0x85), chr(0x2028), chr(0x2029)])
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SPACES_RE = re.compile(r" {2,}")


def clean_text(text: str, *, max_chars: int, keep_newlines: bool = True) -> str:
    """Strip invisible/control characters, normalize whitespace and cap the length."""
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch in _NEWLINES:
            out.append("\n")
        elif ch == "\t" or category in ("Zs", "Zl", "Zp"):
            out.append(" ")
        elif category in _DROP_CATEGORIES:
            continue
        else:
            out.append(ch)
    cleaned = "".join(out)
    if not keep_newlines:
        cleaned = cleaned.replace("\n", " ")
    cleaned = "\n".join(_SPACES_RE.sub(" ", line).strip() for line in cleaned.split("\n"))
    cleaned = _BLANK_LINES_RE.sub("\n\n", cleaned).strip()
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        boundary = max(cut.rfind(" "), cut.rfind("\n"))
        cleaned = (cut[:boundary] if boundary > max_chars // 2 else cut).rstrip()
    return cleaned
