# ruff: noqa: RUF001, RUF003
import pytest

from files_support import fake_hit
from luna.files.query import QueryError, clean_aliases, load_kinds, matches, parse_query
from luna.files.rank import rank, score

KINDS = load_kinds()


def parse(text, **kw):
    return parse_query(text, KINDS, **kw)


def flat(q):
    return [set(variants) for variants in q.tokens]


def test_resume_has_latin_variants():
    q = parse("резюме")
    assert len(q.tokens) == 1
    assert {"резюме", "резюм", "rezyume", "cv", "resume"} <= set(q.tokens[0])
    assert q.extensions is None and not q.dirs_only


def test_report_for_may_tokens_and_month_variants():
    q = parse("отчёт за май")
    assert len(q.tokens) == 2  # "за" is a stop word
    assert {"отчет", "report"} <= set(q.tokens[0])  # ё folded to е
    assert {"май", "мая", "may"} <= set(q.tokens[1])


def test_pdf_kind_word_becomes_extension_filter():
    q = parse("пдф с договором")
    assert q.extensions == frozenset({".pdf"})
    assert len(q.tokens) == 1
    assert "догов" in q.tokens[0]  # 5-letter stem of "договором"


@pytest.mark.parametrize(
    ("text", "ext"),
    [
        ("таблица продажи", ".xlsx"),
        ("презентация проект", ".pptx"),
        ("фото отпуск", ".jpg"),
        ("word отчет", ".docx"),
        ("музыка queen", ".mp3"),
    ],
)
def test_kind_words(text, ext):
    assert ext in parse(text).extensions


def test_folder_word_means_dirs_only():
    q = parse("папку проекты")
    assert q.dirs_only and q.extensions is None
    assert flat(q)[0] >= {"проекты"}


def test_explicit_kind_argument():
    assert parse("договор", kind="pdf").extensions == frozenset({".pdf"})
    assert parse("договор", kind="folder").dirs_only
    assert parse("договор", kind="any").extensions is None
    with pytest.raises(QueryError):
        parse("договор", kind="exe")


def test_at_most_four_tokens():
    assert len(parse("alpha beta gamma delta epsilon zeta").tokens) == 4


def test_year_kept_verbatim():
    assert parse("отчет 2026").tokens[1] == ("2026",)


@pytest.mark.parametrize(
    "text",
    [
        "..\\secret",
        "C:\\Users",
        "a/b",
        "*.pdf",
        "x|y",
        "100%",
        "name\x00",
        "a" * 41,
        "x" * 200,
        "[a]",
    ],
)
def test_invalid_characters_rejected(text):
    with pytest.raises(QueryError):
        parse(text)


@pytest.mark.parametrize("text", ["", "   ", "за в на", "пдф", "папку"])
def test_no_name_words_rejected(text):
    with pytest.raises(QueryError):
        parse(text)


def test_user_alias_adds_variants():
    q = parse("паспорт", aliases=clean_aliases({"паспорт": ["passport", "id"]}))
    assert {"passport", "id"} <= set(q.tokens[0])


def test_bad_alias_table_rejected():
    from luna.core.errors import ConfigError

    with pytest.raises(ConfigError):
        clean_aliases({"x": []})


def test_open_allowlist_and_never_open():
    assert KINDS.can_open(".PDF") and KINDS.can_open(".mp4")
    for ext in (".exe", ".bat", ".lnk", ".ps1", ".py", ".dll", ".unknownext", ""):
        assert not KINDS.can_open(ext)
    assert not (KINDS.open_extensions & KINDS.never_open)


def test_matches_requires_every_token_and_kind():
    q = parse("пдф отчет май")
    assert matches(q, "отчет_май.pdf", is_dir=False, suffix=".pdf")
    assert not matches(q, "отчет_май.docx", is_dir=False, suffix=".docx")
    assert not matches(q, "отчет.pdf", is_dir=False, suffix=".pdf")
    assert not matches(q, "отчет_май.pdf", is_dir=True)


def test_ranking_prefers_the_whole_word():
    q = parse("резюме")
    good = fake_hit(r"C:\d\Резюме_2026.pdf")
    weak = fake_hit(r"C:\d\резюмирование.txt")
    assert score(q, good) > score(q, weak)
    assert rank([weak, good], q, limit=5, min_score=0)[0] == good
    assert rank([weak, good], q, limit=5) == [good]  # the weak one is under the threshold


def test_rank_filters_kind_dedupes_and_limits():
    q = parse("пдф отчет")
    pdf = fake_hit(r"C:\d\Отчет.pdf")
    other = fake_hit(r"C:\d\Отчет.docx")
    folder = fake_hit(r"C:\d\Отчет.pdf", is_dir=True, size=None)
    dup = fake_hit(r"C:\D\ОТЧЕТ.pdf")  # same path, other case
    assert rank([pdf, other, folder, dup], q, limit=5) == [pdf]


def test_ties_go_to_the_more_recent_file():
    q = parse("отчет")
    old = fake_hit(r"C:\a\отчет.txt", day=1)
    new = fake_hit(r"C:\b\отчет.txt", day=20)
    assert rank([old, new], q, limit=2) == [new, old]
    assert rank([old, new], q, limit=1) == [new]


def test_transliterated_and_alias_names_found():
    q = parse("резюме")
    assert rank([fake_hit(r"D:\old\resume_en.docx")], q, limit=3)
    assert rank([fake_hit(r"D:\old\cv.pdf")], q, limit=3)
