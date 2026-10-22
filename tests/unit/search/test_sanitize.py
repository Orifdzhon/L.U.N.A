from luna.search.sanitize import clean_text

ZWSP, ZWNJ, ZWJ, BOM = (chr(0x200B), chr(0x200C), chr(0x200D), chr(0xFEFF))
RLO, LRI, PDI = chr(0x202E), chr(0x2066), chr(0x2069)
TAG_A = chr(0xE0041)
NBSP, LINE_SEP = chr(0xA0), chr(0x2028)


def test_removes_zero_width_bidi_tag_and_control_characters() -> None:
    text = f"a{ZWSP}b{ZWNJ}c{ZWJ}d{BOM}e{RLO}f{LRI}g{PDI}h\x00i\x1bj{TAG_A}k"
    assert clean_text(text, max_chars=100) == "abcdefghijk"


def test_whitespace_normalized_and_newlines_kept() -> None:
    text = f"one \t two{NBSP}three\r\n\r\n\r\n\r\nfour{LINE_SEP}five"
    assert clean_text(text, max_chars=100) == "one two three\n\nfour\nfive"


def test_keep_newlines_false_flattens() -> None:
    assert clean_text("a\nb\n\nc", max_chars=100, keep_newlines=False) == "a b c"


def test_truncates_at_word_boundary() -> None:
    out = clean_text("word " * 100, max_chars=50)
    assert len(out) <= 50
    assert out.endswith("word")
