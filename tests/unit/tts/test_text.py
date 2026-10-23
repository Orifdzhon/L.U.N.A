# ruff: noqa: RUF001
"""Tests for luna.tts.text (speech text preparation)."""

from __future__ import annotations

import re
import time

import pytest

from luna.tts.text import clean_for_speech, prepare_speech, split_sentences

WEATHER = (
    "По данным Яндекс.Погоды, сегодня в Душанбе стоит ясная и малооблачная погода [1]:\n\n"
    "* **Днём:** температура воздуха прогревается до **+21°C**.\n"
    "* **Ночью:** около +9°C."
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Сейчас 13:47 по местному времени (UTC+3).", "Сейчас 13:47 по местному времени (UTC+3)."),
        ("Курс доллара составляет **≈ 81.1** рублей", "Курс доллара составляет 81.1 рублей"),
        ("(по данным yandex.tj)", "(по данным yandex.tj)"),
        ("# Title\n## Sub heading", "Title\nSub heading"),
        ("- one\n* two\n+ three\n• four", "one\ntwo\nthree\nfour"),
        ("It is *really* __very__ ~~bad~~ good", "It is really very bad good"),
        ("snake_case_name stays", "snake_case_name stays"),
        ("See [Wikipedia](https://en.wikipedia.org/wiki/Moon) now", "See Wikipedia now"),
        ("Fact [1] and fact [2, 3].", "Fact and fact."),
        ("Run `ls -la` please", "Run ls -la please"),
        ("Before\n```python\nprint(1)\n```\nAfter", "Before\nAfter"),
        ("> quoted text", "quoted text"),
        ("a\n---\nb", "a\nb"),
        ("| a | b |\n|---|---|\n| 1 | 2 |", "a, b\n1, 2"),
        ("Open https://www.example.com/a?b=1 now", "Open example.com now"),
        ("Go to https://example.com/x.", "Go to example.com."),
        ("Smile \U0001f600 and \U0001f44d\U0001f3fd done \u2705", "Smile and done"),
        ("100\u202f000\u00a0рублей", "100 000 рублей"),
        ("a\u2009b\u200bc\ufeffd", "a bcd"),
        ("  a   b \n\n\n c  ", "a b\nc"),
        ("", ""),
        ("   \n\t ", ""),
    ],
)
def test_clean_for_speech(raw: str, expected: str) -> None:
    assert clean_for_speech(raw) == expected


def test_clean_issue_example() -> None:
    out = clean_for_speech("**Курс** — 95,5 ₽ \U0001f600 https://example.com/a?b=1")
    assert "*" not in out
    assert "\u202f" not in out
    assert "https://" not in out
    assert not re.search("[\U0001f000-\U0001faff]", out)
    assert "95,5 ₽" in out
    assert "example.com" in out


def test_clean_weather_reply() -> None:
    out = clean_for_speech(WEATHER)
    assert "[1]" not in out
    assert "*" not in out
    assert "Днём: температура воздуха прогревается до +21°C." in out
    assert out.startswith(
        "По данным Яндекс.Погоды, сегодня в Душанбе стоит ясная и малооблачная погода:"
    )


@pytest.mark.parametrize(
    "raw",
    [
        WEATHER,
        "**Курс** — 95,5 ₽ \U0001f600 https://example.com/a?b=1",
        "100\u202f000\u00a0рублей",
        "| a | b |\n|---|---|\n| 1 | 2 |",
        "# H\n- *x* and `y` [z](http://q.io) [1]",
        "Plain sentence. Another one!",
    ],
)
def test_clean_idempotent(raw: str) -> None:
    once = clean_for_speech(raw)
    assert clean_for_speech(once) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Привет. Как дела? Отлично!", ["Привет.", "Как дела?", "Отлично!"]),
        ("Hello there. How are you? Fine!", ["Hello there.", "How are you?", "Fine!"]),
        ("Wait… Really? Yes.", ["Wait…", "Really?", "Yes."]),
        ("Строка один\nСтрока два", ["Строка один", "Строка два"]),
        ("Это т.е. пример, т.д. и т.п. тоже.", ["Это т.е. пример, т.д. и т.п. тоже."]),
        ("В 2020 г. вышла книга. Она хорошая.", ["В 2020 г. вышла книга.", "Она хорошая."]),
        ("Живу на ул. Ленина, д. 5. Пока.", ["Живу на ул. Ленина, д. 5.", "Пока."]),
        ("Use e.g. a cat. Or Dr. Smith.", ["Use e.g. a cat.", "Or Dr. Smith."]),
        ("А. С. Пушкин родился давно. Да.", ["А. С. Пушкин родился давно.", "Да."]),
        ("Курс 3.5 рубля. Дальше 95,5 ₽.", ["Курс 3.5 рубля.", "Дальше 95,5 ₽."]),
        ("1. Первый\n2. Второй", ["1. Первый", "2. Второй"]),
        ('Он сказал "стоп." Потом ушёл.', ['Он сказал "стоп."', "Потом ушёл."]),
        ("", []),
        ("   \n  ", []),
    ],
)
def test_split_sentences(text: str, expected: list[str]) -> None:
    assert split_sentences(text, first_max_chars=100, max_chars=1000) == expected


def test_overlong_sentence_wrapped_at_commas() -> None:
    sentence = ", ".join([f"слово номер {i}" for i in range(30)]) + "."
    chunks = split_sentences(sentence, first_max_chars=40, max_chars=10_000, max_chunk_chars=80)
    assert len(chunks) > 3
    assert len(chunks[0]) <= 40
    assert all(len(c) <= 80 for c in chunks)
    assert " ".join(chunks).replace(" ", "") == sentence.replace(" ", "")
    assert chunks[0].endswith(",")


def test_first_chunk_short_and_later_chunks_bounded() -> None:
    text = ("Это довольно длинное предложение без запятых " * 10).strip() + ". Коротко."
    chunks = split_sentences(text, first_max_chars=50, max_chars=10_000, max_chunk_chars=100)
    assert len(chunks[0]) <= 50
    assert all(len(c) <= 100 for c in chunks[1:])
    assert chunks[-1] == "Коротко."


def test_single_long_word_not_cut() -> None:
    word = "а" * 80
    assert split_sentences(f"{word} конец.", first_max_chars=20, max_chars=1000) == [word, "конец."]
    assert split_sentences(word, first_max_chars=20, max_chars=1000) == [word]


def test_total_truncated_on_chunk_boundary() -> None:
    text = "Один два три. Четыре пять шесть. Семь восемь девять."
    chunks = split_sentences(text, first_max_chars=100, max_chars=32)
    assert chunks == ["Один два три.", "Четыре пять шесть."]


def test_total_keeps_at_least_one_chunk() -> None:
    assert split_sentences("Длинное первое предложение.", first_max_chars=100, max_chars=5) == [
        "Длинное первое предложение."
    ]


def test_prepare_speech_weather() -> None:
    chunks = prepare_speech(WEATHER, first_max_chars=60)
    assert len(chunks[0]) <= 60
    joined = " ".join(chunks)
    assert "*" not in joined
    assert "[1]" not in joined
    assert "+21°C." in joined


def test_prepare_speech_en_and_empty() -> None:
    chunks = prepare_speech(
        "Emmanuel Macron is the president of France (per Wikipedia). It is **true**."
    )
    assert chunks == ["Emmanuel Macron is the president of France (per Wikipedia).", "It is true."]
    assert prepare_speech("") == []
    assert prepare_speech("\U0001f600 \u202f ") == []


def test_year_followed_by_capital_splits_after_g() -> None:
    assert split_sentences("Это было в 1799 г. Это так.", first_max_chars=100, max_chars=1000) == [
        "Это было в 1799 г.",
        "Это так.",
    ]
    # Not a year, or lower-case continuation: still an abbreviation.
    assert split_sentences("Во 99 г. Это так.", first_max_chars=100, max_chars=1000) == [
        "Во 99 г. Это так."
    ]
    assert split_sentences("в 1799 г. вышла книга.", first_max_chars=100, max_chars=1000) == [
        "в 1799 г. вышла книга."
    ]


HOSTILE = {
    "brackets": "[" * 200_000,
    "dots": "a. " * 66_000,
    "blank_lines": " \n" * 100_000,
    "bold": "**a " * 50_000,
    "link_open": "[a](" * 50_000,
    "nested": "[" * 100_000 + "x" + "]" * 100_000,
    "ellipsis": "." * 200_000 + "x",
    "backticks": "`a" * 100_000,
    "italic": "*a " * 66_000,
    "underscore": "_a " * 66_000,
    "table": "|" + "-" * 200_000 + "x",
    "dashes": "- " * 100_000 + "x",
}


@pytest.mark.parametrize("name", list(HOSTILE))
def test_hostile_input_is_fast(name: str) -> None:
    text = HOSTILE[name]
    for func in (clean_for_speech, prepare_speech):
        start = time.perf_counter()
        func(text)
        assert time.perf_counter() - start < 0.2, (name, func.__name__)
