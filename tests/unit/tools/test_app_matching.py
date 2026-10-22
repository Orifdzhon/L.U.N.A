import pytest

from luna.core.errors import ConfigError
from luna.platform.base import AppEntry
from luna.tools.app_matching import (
    MatchStatus,
    default_app_aliases,
    match_app,
    merge_aliases,
    normalize_query,
    transliterate,
)

APPS = [
    AppEntry("Telegram", "Telegram.TelegramDesktop"),
    AppEntry("Google Chrome", "Chrome"),
    AppEntry("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    AppEntry("Блокнот", "{1AC14E77}\\notepad.exe"),
    AppEntry("Microsoft Edge", "MSEdge"),
]
ALIASES = default_app_aliases()


def resolve(name: str, apps: list[AppEntry] = APPS) -> str | None:
    res = match_app(name, apps, ALIASES)
    return res.entry.name if res.status is MatchStatus.MATCH and res.entry else None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("telegram", "Telegram"),
        ("Telegram", "Telegram"),
        ("телеграм", "Telegram"),
        ("телега", "Telegram"),
        ("хром", "Google Chrome"),
        ("chrome", "Google Chrome"),
        ("google chrome", "Google Chrome"),
        ("калькулятор", "Калькулятор"),
        ("Калькулятор", "Калькулятор"),
        ("calculator", "Калькулятор"),
        ("блокнот", "Блокнот"),
        ("notepad", "Блокнот"),
        ("edge", "Microsoft Edge"),
        ("открой Telegram", "Telegram"),
        ("запусти хром пожалуйста", "Google Chrome"),
        ("open the calculator please", "Калькулятор"),
    ],
)
def test_resolves(name: str, expected: str) -> None:
    assert resolve(name) == expected


def test_alias_falls_back_to_english_target() -> None:
    apps = [AppEntry("Calculator", "calc"), AppEntry("Notepad", "np")]
    assert resolve("калькулятор", apps) == "Calculator"
    assert resolve("блокнот", apps) == "Notepad"


def test_fuzzy_typos_and_transliteration() -> None:
    assert resolve("telegramm") == "Telegram"
    assert resolve("калькулятр") == "Калькулятор"


def test_yo_is_folded() -> None:
    apps = [AppEntry("Ёлка", "elka")]
    assert resolve("елка", apps) == "Ёлка"


def test_threshold_is_configurable() -> None:
    assert match_app("telegrm", APPS, ALIASES, threshold=85).status is MatchStatus.MATCH
    assert match_app("telegrm", APPS, ALIASES, threshold=100).status is MatchStatus.NONE


@pytest.mark.parametrize("name", ["фотошоп", "zzzzzz", "", "   ", "я", "открой"])
def test_no_match(name: str) -> None:
    assert match_app(name, APPS, ALIASES).status is MatchStatus.NONE


def test_empty_catalog() -> None:
    assert match_app("telegram", [], ALIASES).status is MatchStatus.NONE


def test_alias_to_missing_app_does_not_match_something_else() -> None:
    assert match_app("хром", [AppEntry("Блокнот", "n")], ALIASES).status is MatchStatus.NONE


@pytest.mark.parametrize(
    "name",
    [
        "C:\\Windows\\System32\\cmd.exe",
        "cmd /c del *",
        "\\\\server\\share\\x.exe",
        "powershell -c calc; calc",
        "notepad && calc",
        "cmd.exe",
        "shell:AppsFolder\\Chrome",
        "x" * 500,
    ],
)
def test_paths_and_command_lines_never_match(name: str) -> None:
    apps = [*APPS, AppEntry("Command Prompt", "cmd"), AppEntry("Windows PowerShell", "ps")]
    assert match_app(name, apps, ALIASES).status is MatchStatus.NONE


def test_near_equal_candidates_are_ambiguous() -> None:
    apps = [AppEntry("Visual Studio 2019", "vs19"), AppEntry("Visual Studio 2022", "vs22")]
    res = match_app("visual studio", apps, {})
    assert res.status is MatchStatus.AMBIGUOUS
    assert res.entry is None
    assert {e.name for e in res.candidates} == {"Visual Studio 2019", "Visual Studio 2022"}


def test_same_name_different_ids_collapses_to_lowest_id() -> None:
    apps = [AppEntry("Steam", "b"), AppEntry("Steam", "a")]
    res = match_app("steam", apps, {})
    assert res.status is MatchStatus.MATCH
    assert res.entry == AppEntry("Steam", "a")


def test_duplicate_app_id_is_one_app() -> None:
    apps = [AppEntry("Steam", "a"), AppEntry("Steam", "a")]
    assert match_app("steam", apps, {}).status is MatchStatus.MATCH


def test_clear_lead_over_longer_name_matches() -> None:
    apps = [AppEntry("Telegram", "t"), AppEntry("Telegram Web Helper", "w")]
    assert resolve("telegram", apps) == "Telegram"


def test_margin_is_configurable() -> None:
    apps = [AppEntry("Telegram Desktop", "t"), AppEntry("Telegram Web", "w")]
    assert match_app("telegram", apps, {}, margin=5).status is MatchStatus.AMBIGUOUS
    assert match_app("telegram", apps, {}, margin=0).status is MatchStatus.AMBIGUOUS
    one = match_app("telegram desktop", apps, {}, margin=5)
    assert one.status is MatchStatus.MATCH


def test_only_catalog_entries_are_returned() -> None:
    res = match_app("telegram", APPS, ALIASES)
    assert res.entry in APPS


def test_entry_aliases_are_used() -> None:
    apps = [AppEntry("Some Long Name", "x", aliases=("sln",))]
    assert resolve("sln", apps) == "Some Long Name"


def test_normalize_and_transliterate() -> None:
    assert normalize_query("  Открой, ПОЖАЛУЙСТА   Телеграм! ") == "телеграм"
    assert normalize_query("Ёжик") == "ежик"
    assert transliterate("телеграм") == "telegram"


def test_user_aliases_override_defaults() -> None:
    aliases = merge_aliases({"хром": "Microsoft Edge", "Мессенджер": ["Telegram"]})
    assert match_app("хром", APPS, aliases).entry == APPS[4]
    assert match_app("мессенджер", APPS, aliases).entry == APPS[0]


@pytest.mark.parametrize("bad", [{"": "x"}, {"a": ""}, {"a": []}, {"a": 1}, {"a": ["ok", 2]}])
def test_invalid_user_aliases(bad: dict) -> None:
    with pytest.raises(ConfigError):
        merge_aliases(bad)
