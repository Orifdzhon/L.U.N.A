# ruff: noqa: RUF001, RUF002, RUF003
"""Text preparation for speech: clean an LLM reply and split it into speakable chunks.

Pure functions, no I/O. Speech starts after the first (short) chunk is synthesized,
so the first chunk is kept small (ADR 0005).
"""

from __future__ import annotations

import re

__all__ = ["clean_for_speech", "prepare_speech", "split_sentences"]

# --- character-level clean-up -------------------------------------------------

# Space-like characters that SAPI reads badly or skips -> plain space.
_SPACE_CHARS = (
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u202f\u205f\u3000\t\r\x0b\x0c"
)
_SPACE_TABLE = {ord(c): " " for c in _SPACE_CHARS}
# Zero-width / invisible formatting characters -> removed.
_INVISIBLE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad\ufe0e\ufe0f\u20e3]"
)
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff"  # pictographs, emoticons, flags, symbols
    "\u2190-\u21ff"  # arrows
    "\u2300-\u23ff"  # misc technical
    "\u2460-\u24ff"  # enclosed alphanumerics
    "\u25a0-\u27bf"  # geometric shapes, misc symbols, dingbats
    "\u2900-\u297f\u2b00-\u2bff"  # supplemental arrows, misc symbols
    "\U000e0000-\U000e007f]"  # tags
)
# Signs that TTS engines skip or mispronounce; the number stays, the sign goes.
_SYMBOL_DROP_RE = re.compile(r"[≈~] ?")

# --- markdown -----------------------------------------------------------------

_FENCE_RE = re.compile(r"(```|~~~).*?(?:\1|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`+([^`\n]*)`+")
_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\([^)\n]*\)")
_CITATION_RE = re.compile(r" ?\[\d+(?:\s*[,-]\s*\d+)*\]")
_URL_RE = re.compile(r"""(?:https?://|www\.)[^\s<>"'«»]+""", re.IGNORECASE)
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+•\u25aa\u25cf\u25e6‣]\s+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s*>+\s?", re.MULTILINE)
_HRULE_RE = re.compile(r"^[ ]*([-*_=])(?:[ ]*\1){2,}[ ]*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^[ ]*\|?[ :|-]*-[ :|-]*\|?[ ]*$")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"\*(?=\S)([^*\n]+?)(?<=\S)\*")
_ITALIC_UNDER_RE = re.compile(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)")
_URL_TRAILING = ".,;:!?)]}»"


def _url_to_domain(match: re.Match[str]) -> str:
    url = match.group(0)
    stripped = url.rstrip(_URL_TRAILING)
    trailing = url[len(stripped) :]
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", stripped, flags=re.IGNORECASE)
    host = re.split(r"[/?#]", host, maxsplit=1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    # Keep sentence punctuation that followed the URL ("see https://a.com/x.").
    return host + trailing


def _table_lines(text: str) -> str:
    out: list[str] = []
    for line in text.split("\n"):
        if line.lstrip().startswith("|") and line.count("|") >= 2:
            if _TABLE_SEP_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = ", ".join(c for c in cells if c)
        out.append(line)
    return "\n".join(out)


def clean_for_speech(text: str) -> str:
    """Strip markdown, URLs, emoji and odd whitespace so TTS reads the text cleanly.

    Single newlines are kept (as line breaks) for sentence splitting.
    The function is idempotent.
    """
    text = text.translate(_SPACE_TABLE)
    text = _INVISIBLE_RE.sub("", text)

    text = _FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(_url_to_domain, text)
    text = _CITATION_RE.sub("", text)

    text = _HRULE_RE.sub("", text)
    text = _table_lines(text)
    text = _HEADING_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    text = text.replace("*", "")

    text = _EMOJI_RE.sub("", text)
    text = _SYMBOL_DROP_RE.sub("", text)

    lines = (re.sub(r" {2,}", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


# --- sentence splitting ---------------------------------------------------------

# Lower-case tokens (without the final dot) after which a dot does not end a sentence.
_ABBREVIATIONS = frozenset(
    {
        # Russian
        "т.е", "т.д", "т.п", "т.к", "т.н", "т.ч", "т.о", "и.о", "г", "гг", "ул", "пр",
        "просп", "пл", "д", "кв", "корп", "стр", "им", "др", "проф", "акад", "доц", "см",
        "рис", "табл", "руб", "коп", "тыс", "млн", "млрд", "обл", "пос", "ст", "св", "тел",
        "напр", "прим", "ред",
        # English
        "e.g", "i.e", "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "approx",
        "inc", "ltd", "co", "a.m", "p.m", "u.s", "fig", "vol", "dept",
    }
)  # fmt: skip
_BOUNDARY_RE = re.compile(r"([.!?…]+)([\"'»)\]”’]*)(?:\s+|$)")
_OPENERS = "(\"'«[“‘"


def _is_abbreviation(text: str, dot_pos: int) -> bool:
    """True if the dot at ``dot_pos`` closes an abbreviation, an initial or a list number."""
    head = text[:dot_pos]
    token = re.split(r"\s", head)[-1].lstrip(_OPENERS).lower()
    if not token:
        return False
    if token in _ABBREVIATIONS:
        return True
    if token.isdigit() and len(token) <= 2 and head[: len(head) - len(token)].strip() == "":
        return True  # list number such as "1. Foo"
    # Single-letter initial ("А. С. Пушкин"), but not the one-letter words "я" / "I".
    return len(token) == 1 and token.isalpha() and token not in ("я", "a", "i")


def _split_line(line: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for m in _BOUNDARY_RE.finditer(line):
        if m.group(1) == "." and not m.group(2) and _is_abbreviation(line, m.start(1)):
            continue
        sentences.append(line[start : m.end(2)])
        start = m.end()
    if start < len(line):
        sentences.append(line[start:])
    return [s.strip() for s in sentences if s.strip()]


def _break_point(text: str, limit: int) -> int:
    """Index at which to cut ``text`` so the head is <= limit (unless one word is longer)."""
    window = text[: limit + 1]
    comma = max(window.rfind(", "), window.rfind("; "), window.rfind(": "))
    if comma >= limit // 3:
        return comma + 1
    space = window.rfind(" ")
    if space > 0:
        return space
    space = text.find(" ", limit)
    return space if space > 0 else len(text)


def split_sentences(
    text: str, *, first_max_chars: int, max_chars: int, max_chunk_chars: int = 220
) -> list[str]:
    """Split ``text`` into speakable chunks.

    - boundaries: ``. ! ? …`` followed by whitespace, and newlines; common abbreviations
      ("т.е.", "г.", "e.g."), initials, list numbers and decimals do not split;
    - the first chunk is at most ``first_max_chars`` (unless a single word is longer),
      later chunks at most ``max_chunk_chars``; overlong sentences are wrapped at
      commas, then spaces;
    - the total is truncated at ``max_chars`` on a chunk boundary (at least one chunk
      is always kept).
    """
    chunks: list[str] = []
    total = 0
    for raw_line in text.split("\n"):
        for sentence in _split_line(" ".join(raw_line.split())):
            rest = sentence
            while rest:
                limit = first_max_chars if not chunks else max_chunk_chars
                if len(rest) <= limit:
                    piece, rest = rest, ""
                else:
                    cut = _break_point(rest, limit)
                    piece, rest = rest[:cut].strip(), rest[cut:].strip()
                if not piece:
                    continue
                if chunks and total + len(piece) > max_chars:
                    return chunks
                chunks.append(piece)
                total += len(piece)
    return chunks


def prepare_speech(
    text: str,
    *,
    first_max_chars: int = 120,
    max_chars: int = 1500,
    max_chunk_chars: int = 220,
) -> list[str]:
    """Clean ``text`` and split it into chunks for the speaker."""
    return split_sentences(
        clean_for_speech(text),
        first_max_chars=first_max_chars,
        max_chars=max_chars,
        max_chunk_chars=max_chunk_chars,
    )
