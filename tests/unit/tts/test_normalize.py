# ruff: noqa: RUF001
"""Tests for luna.tts.normalize (text rewriting for local voices)."""

from __future__ import annotations

import time

import pytest

from luna.tts.normalize import normalize_for_speech, number_to_words


def ru(text: str) -> str:
    return normalize_for_speech(text, "ru")


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (0, "ноль"),
        (1, "один"),
        (11, "одиннадцать"),
        (21, "двадцать один"),
        (100, "сто"),
        (115, "сто пятнадцать"),
        (1000, "одна тысяча"),
        (2000, "две тысячи"),
        (5000, "пять тысяч"),
        (21000, "двадцать одна тысяча"),
        (79764, "семьдесят девять тысяч семьсот шестьдесят четыре"),
        (1_000_000, "один миллион"),
        (2_500_003, "два миллиона пятьсот тысяч три"),
        (-5, "минус пять"),
    ],
)
def test_number_to_words(number, words):
    assert number_to_words(number) == words


def test_feminine_numbers():
    assert number_to_words(1, feminine=True) == "одна"
    assert number_to_words(22, feminine=True) == "двадцать две"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("79 764 ₽", "семьдесят девять тысяч семьсот шестьдесят четыре рубля"),
        (
            "Это стоит 79 764 ₽.",
            "Это стоит семьдесят девять тысяч семьсот шестьдесят четыре рубля.",
        ),
        ("1 ₽", "один рубль"),
        ("5 ₽", "пять рублей"),
        ("11 ₽", "одиннадцать рублей"),
        ("22 ₽", "двадцать два рубля"),
        ("100 руб.", "сто рублей"),
        ("$5", "пять долларов"),
        ("10 €", "десять евро"),
        ("25%", "двадцать пять процентов"),
        ("1%", "один процент"),
        ("Скидка 3 %", "Скидка три процента"),
        ("3,5%", "три целых пять десятых процента"),
        ("1,5 ₽", "одна целая пять десятых рубля"),
        ("2.25", "две целых двадцать пять сотых"),
        ("-5 градусов", "минус пять градусов"),
        ("12:30", "двенадцать тридцать"),
        ("9:05", "девять ноль пять"),
        ("15:00", "пятнадцать часов"),
        ("в 2024 году", "в две тысячи двадцать четвёртом году"),
        ("с 2000 года", "с двухтысячного года"),
        ("007", "ноль ноль семь"),
        ("5.0 ₽", "пять рублей"),
    ],
)
def test_numbers_and_symbols(text, expected):
    assert ru(text) == expected


def test_digit_runs_inside_words_are_left_alone():
    assert "mp3" not in ru("файл mp3")  # Latin part is transliterated, digits stay glued
    assert ru("H2O") == ru("H2O")  # no crash on mixed tokens


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("YouTube", "ютуб"),
        ("youtube", "ютуб"),
        ("Открой Instagram", "Открой инстаграм"),
        ("Google", "гугл"),
        ("Telegram", "телеграм"),
        ("API", "эй-пи-ай"),
        ("USB", "ю-эс-би"),
        ("Wi-Fi", "вай-фай"),
        ("iPhone 15", "айфон пятнадцать"),
    ],
)
def test_brands_and_acronyms(text, expected):
    assert ru(text) == expected


def test_unknown_latin_words_are_transliterated():
    out = ru("Kubernetes")
    assert out and all(not c.isascii() for c in out)
    assert ru("shop") == "шоп"
    assert ru("Check") == "чек"


def test_cyrillic_text_is_untouched():
    assert ru("Привет, как дела?") == "Привет, как дела?"


def test_english_expands_symbols_only():
    assert normalize_for_speech("Pay $10 now", "en") == "Pay 10 dollars now"
    assert normalize_for_speech("25% off", "en") == "25 percent off"
    assert normalize_for_speech("Hello YouTube", "en") == "Hello YouTube"


def test_unknown_language_is_unchanged():
    assert normalize_for_speech("5 ₽", "de") == "5 ₽"


def test_huge_numbers_are_read_digit_by_digit():
    assert ru("1234567890123456").startswith("один два три")


def test_idempotent_on_normalised_text():
    once = ru("Скидка 25% на YouTube")
    assert ru(once) == once


def test_pathological_input_is_fast():
    start = time.monotonic()
    ru(("9 " * 5000) + ("a" * 5000) + " " + ("1 234 " * 2000))
    assert time.monotonic() - start < 2.0
