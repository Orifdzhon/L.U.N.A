"""Regression tests on names from a real Start menu (wrong-app launches are the worst failure)."""

import pytest

from luna.platform.base import AppEntry
from luna.tools.app_matching import MatchStatus, default_app_aliases, match_app

NAMES = [
    "Управление печатью", "Uninstall Zoom Workplace", "Zoom Workplace", "FL Studio 2026",
    "LM Studio", "Visual Studio Code", "Epic Games Launcher", "Rockstar Games Launcher",
    "Solitaire & Casual Games", "Python 3.13 (64-bit)", "Python 3.13 Manuals (64-bit)",
    "Python 3.13 Module Docs (64-bit)", "WordPad", "Параметры", "Корзина", "Блокнот",
    "Калькулятор", "Google Chrome", "Telegram", "Discord", "Steam", "Проводник",
    "Яндекс Музыка", "Яндекс Браузер", "Outlook", "OneNote", "Диктофон", "Карты", "Chat",
    "Notepad++", "Battle.net",
]  # fmt: skip
APPS = [AppEntry(n, f"id{i}") for i, n in enumerate(NAMES)]
ALIASES = default_app_aliases()


def outcome(query: str, apps: list[AppEntry] = APPS) -> tuple[MatchStatus, tuple[str, ...]]:
    res = match_app(query, apps, ALIASES)
    names = (res.entry.name,) if res.entry else tuple(e.name for e in res.candidates)
    return res.status, names


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("zoom", "Zoom Workplace"),
        ("fl studio", "FL Studio 2026"),
        ("фл студио", "FL Studio 2026"),
        ("epic games", "Epic Games Launcher"),
        ("python", "Python 3.13 (64-bit)"),
        ("хром", "Google Chrome"),
        ("телега", "Telegram"),
        ("telegram", "Telegram"),
        ("открой калькулятор", "Калькулятор"),
        ("блокнот", "Блокнот"),
        ("проводник", "Проводник"),
        ("корзина", "Корзина"),
        ("steam", "Steam"),
        ("discord", "Discord"),
        ("дискорд", "Discord"),
        ("notepad++", "Notepad++"),
        ("Battle.net", "Battle.net"),
        ("яндекс музыка", "Яндекс Музыка"),
        ("запусти пожалуйста телеграм пожалуйста", "Telegram"),
    ],
)  # fmt: skip
def test_resolves(query: str, expected: str) -> None:
    assert outcome(query) == (MatchStatus.MATCH, (expected,))


@pytest.mark.parametrize(
    "query",
    [
        "чат", "окно", "блок", "tel", "калк", "ок", "он", "ты", "музыка", "браузер",
        "код", "игра", "спотифай", "print",
    ],
)  # fmt: skip
def test_partial_or_generic_queries_match_nothing(query: str) -> None:
    status, names = outcome(query)
    assert status is not MatchStatus.MATCH, names


@pytest.mark.parametrize(
    "query",
    [
        "закрой телеграм", "удали телеграм", "хром и телеграм", "хром, телеграм",
        "steam update", "uninstall zoom", "close chrome", "telegram and discord",
    ],
)  # fmt: skip
def test_other_intents_are_unsupported(query: str) -> None:
    assert outcome(query) == (MatchStatus.UNSUPPORTED, ())


def test_uninstallers_are_never_candidates() -> None:
    only = [AppEntry("Uninstall Zoom Workplace", "u"), AppEntry("Удалить Telegram", "t")]
    assert match_app("zoom", only, {}).status is MatchStatus.NONE
    assert match_app("telegram", only, {}).status is MatchStatus.NONE
    by_id = [AppEntry("Zoom", r"C:\Zoom\uninstall.exe")]
    assert match_app("zoom", by_id, {}).status is MatchStatus.NONE


def test_docs_entries_lose_to_executables_but_work_alone() -> None:
    assert outcome("python") == (MatchStatus.MATCH, ("Python 3.13 (64-bit)",))
    docs = [AppEntry("Python 3.13 Manuals (64-bit)", "m")]
    assert match_app("python", docs, {}).status is MatchStatus.MATCH
    assert outcome("python manuals") == (MatchStatus.MATCH, ("Python 3.13 Manuals (64-bit)",))


def test_full_token_match_beats_shared_word() -> None:
    assert outcome("lm studio") == (MatchStatus.MATCH, ("LM Studio",))
    status, names = outcome("studio")
    assert status is MatchStatus.AMBIGUOUS  # never guess among shared-word candidates
    assert {"FL Studio 2026", "LM Studio", "Visual Studio Code"} <= set(names)


def test_alias_targets_use_strict_matching() -> None:
    word = [AppEntry("Microsoft Word", "w"), AppEntry("WordPad", "p")]
    assert match_app("ворд", word, ALIASES).entry == word[0]
    assert match_app("ворд", [AppEntry("WordPad", "p")], ALIASES).status is MatchStatus.NONE


def test_identical_display_names_collapse_deterministically() -> None:
    apps = [AppEntry("Steam", "b"), AppEntry("Steam", "a")]
    res = match_app("steam", apps, {})
    assert res.status is MatchStatus.MATCH
    assert res.entry is not None
    assert res.entry.app_id == "a"


def test_leading_verbs_only_so_apps_named_run_or_start_work() -> None:
    apps = [AppEntry("Start", "s"), AppEntry("My Files", "f")]
    assert match_app("open start", apps, {}).entry == apps[0]
    assert match_app("my files", apps, {}).entry == apps[1]
