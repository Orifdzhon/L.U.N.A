import pytest

from luna.platform.base import AppEntry, RunningApp
from luna.tools.app_matching import MatchStatus, match_app, merge_aliases
from luna.tools.running_match import match_running

ALIASES = merge_aliases()
CATALOG = [
    AppEntry("Telegram", "TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm!Telegram"),
    AppEntry("Google Chrome", "Chrome"),  # "simple" id: no process identity
    AppEntry("Блокнот", "C:\\Windows\\System32\\notepad.exe"),
    AppEntry("Discord", "D:\\Apps\\Discord\\Discord.exe"),
]


def app(key, name, path=None, aumid=None):
    return RunningApp(key, name, path, aumid, (1,), 1)


TELEGRAM = app("tg", "Telegram Desktop", "C:\\x\\Telegram.exe", CATALOG[0].app_id)
CHROME = app("ch", "Google Chrome", "C:\\Chrome\\chrome.exe")
NOTEPAD = app("np", "Блокнот", "C:\\Windows\\System32\\notepad.exe")
OTHER = app("ot", "Some Tool", "D:\\Tools\\sometool.exe")
RUNNING = [TELEGRAM, CHROME, NOTEPAD, OTHER]


def identity(a: RunningApp, e: AppEntry) -> bool:
    return (a.app_id or a.exe_path or "").casefold() == e.app_id.casefold()


def find(name, running=RUNNING, catalog=CATALOG):
    return match_running(name, running, catalog, ALIASES, identity)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("закрой телеграм", TELEGRAM),  # catalog entry -> AUMID
        ("закрой Telegram", TELEGRAM),
        ("выйди из телеграма", TELEGRAM),
        ("close telegram please", TELEGRAM),
        ("закрой блокнот", NOTEPAD),  # catalog entry -> exe path
        ("закрой хром", CHROME),  # alias + "simple" id -> exe description
        ("закрой google chrome", CHROME),
        ("закрой sometool", OTHER),  # not in the catalog: exe stem
        ("quit some tool", OTHER),  # not in the catalog: description
    ],
)
def test_resolves(name, expected):
    result = find(name)
    assert result.status is MatchStatus.MATCH
    assert result.app == expected


def test_installed_but_not_running():
    result = find("закрой discord")
    assert result.status is MatchStatus.NONE
    assert result.entry == CATALOG[3]


def test_unknown_app():
    result = find("закрой фотошоп")
    assert result.status is MatchStatus.NONE
    assert result.entry is None


def test_window_titles_are_not_used():
    # Decoy: a browser tab may be titled "Telegram", but a RunningApp carries no title at all,
    # so "закрой телеграм" cannot reach the browser.
    decoy = RunningApp("ch", "Google Chrome", "C:\\Chrome\\chrome.exe", None, (1,), 1)
    assert not hasattr(decoy, "title")
    result = find("закрой телеграм", running=[decoy, NOTEPAD])
    assert result.status is MatchStatus.NONE


def test_several_running_matches_are_ambiguous():
    a = app("a1", "Visual Tool One", "D:\\a\\one.exe")
    b = app("b1", "Visual Tool Two", "D:\\b\\two.exe")
    result = find("закрой visual tool", running=[a, b], catalog=[])
    assert result.status is MatchStatus.AMBIGUOUS
    assert set(result.candidates) == {a, b}


def test_display_name_prefers_the_catalog_name():
    result = find("закрой телеграм")
    assert result.names == {
        "tg": "Telegram",
        "ch": "Google Chrome",  # not mapped by identity: its own name
        "np": "Блокнот",
        "ot": "Some Tool",
    }


@pytest.mark.parametrize("name", ["закрой телеграм и хром", "закрой, телеграм", "kill telegram"])
def test_not_a_single_close_request(name):
    assert find(name).status is MatchStatus.UNSUPPORTED


def test_verbs_close_in_match_app_and_open_unchanged():
    assert match_app("закрой телеграм", CATALOG, ALIASES).status is MatchStatus.UNSUPPORTED
    assert match_app("закрой телеграм", CATALOG, ALIASES, verbs="close").entry == CATALOG[0]
    assert match_app("открой телеграм", CATALOG, ALIASES, verbs="close").entry == CATALOG[0]
    assert match_app("close", CATALOG, ALIASES, verbs="close").status is MatchStatus.NONE
