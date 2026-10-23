# ruff: noqa: RUF001
import time
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeAppCatalog, FakeBrowser, FakeLauncher, FakeTool
from luna.core.models import Request
from luna.core.router import Router, detect_lang, needs_fresh_facts, parse_open_command
from luna.logs.action_log import ActionLog
from luna.platform.base import AppEntry
from luna.tools.base import ToolResult
from luna.tools.builtin.open_app import OpenAppTool
from luna.tools.builtin.open_website import OpenWebsiteTool
from luna.tools.registry import ToolRegistry


def fake(name: str, result: ToolResult | None = None) -> FakeTool:
    return FakeTool(name, result, parameters={"type": "object", "additionalProperties": True})


APPS = [
    AppEntry("Telegram", "Telegram.TelegramDesktop"),
    AppEntry("Google Chrome", "Chrome"),
    AppEntry("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    AppEntry("Блокнот", "notepad"),
    AppEntry("Microsoft Edge", "MSEdge"),
]


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.catalog = FakeAppCatalog(APPS)
        self.launcher = FakeLauncher(self.catalog)
        self.browser = FakeBrowser()
        self.log_path = tmp_path / "actions.jsonl"
        self.log = ActionLog(self.log_path)
        self.registry = ToolRegistry(self.log)
        self.registry.register(OpenWebsiteTool(self.browser))
        self.registry.register(OpenAppTool(self.catalog, self.launcher))
        self.router = Router(self.registry)

    def route(self, text: str, **kw: Any) -> Any:
        return self.router.route(Request(text, "text", **kw))

    def launched(self) -> list[str]:
        return [a.name for a in self.launcher.launched]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


YT = "https://www.youtube.com"

# (text, ("site", url) | ("app", name) | None for "not handled")
CASES: list[tuple[str, tuple[str, str] | None]] = [
    # --- handled: sites
    ("открой ютуб", ("site", YT)),
    ("Открой ютуб.", ("site", YT)),
    ("Луна, открой ютуб", ("site", YT)),
    ("луна открой ютуб", ("site", YT)),
    ("Luna, open youtube", ("site", YT)),
    ("open YouTube", ("site", YT)),
    ("Open YouTube please", ("site", YT)),
    ("please open youtube", ("site", YT)),
    ("открой, пожалуйста, ютуб", ("site", YT)),
    ("пожалуйста открой ютуб", ("site", YT)),
    ("Луна, пожалуйста, открой ютуб", ("site", YT)),
    ("открой youtube.com", ("site", "https://youtube.com")),
    ("открой https://github.com", ("site", "https://github.com")),
    ("открой гитхаб", ("site", "https://github.com")),
    ("открой мне хабр", ("site", "https://habr.com")),
    ("запусти ютуб", ("site", YT)),
    ("включи ютуб", ("site", YT)),
    ("open habr.com", ("site", "https://habr.com")),
    ("launch github", ("site", "https://github.com")),
    ("открой сайт хабр", ("site", "https://habr.com")),
    # --- handled: apps
    ("запусти хром пожалуйста", ("app", "Google Chrome")),
    ("запусти хром", ("app", "Google Chrome")),
    ("Луна, запусти хром", ("app", "Google Chrome")),
    ("open telegram", ("app", "Telegram")),
    ("Open Telegram, please.", ("app", "Telegram")),
    ("Luna, launch chrome", ("app", "Google Chrome")),
    ("start notepad", ("app", "Блокнот")),
    ("открой калькулятор", ("app", "Калькулятор")),
    ("открой телеграм", ("app", "Telegram")),
    ("открой приложение блокнот", ("app", "Блокнот")),
    ("open the calculator", ("app", "Калькулятор")),
    # --- not handled: questions / search / vague
    ("что такое черная дыра?", None),
    ("какой курс доллара?", None),
    ("какой курс доллара", None),
    ("what is the weather today", None),
    ("открой мне что-нибудь интересное", None),
    ("open something interesting", None),
    ("найди рецепт борща", None),
    ("расскажи анекдот", None),
    ("как открыть ютуб?", None),
    ("можно открыть ютуб?", None),
    ("who is the president", None),
    # --- not handled: negation, other verbs, multi-intent, extra content
    ("не открывай ютуб", None),
    ("не открой ютуб", None),
    ("open youtube, no wait, don't", None),
    ("don't open telegram", None),
    ("закрой телеграм", None),
    ("close chrome", None),
    ("хром и телеграм", None),
    ("запусти хром и телеграм", None),
    ("open chrome and telegram", None),
    ("открой ютуб и найди котиков", None),
    ("открой ютуб, потом гитхаб", None),
    ("открой ютуб там смешные котики", None),
    ("open youtube and play some music", None),
    ("открой хром или эдж", None),
    # --- handled: quoted targets
    ('open "youtube"', ("site", YT)),
    ("открой «ютуб»", ("site", YT)),
    # --- not handled: look-alike verbs/targets that must never launch something
    ("включи свет", None),
    ("включи музыку", None),
    ("включи режим", None),
    ("включи игровой режим", None),
    ("open the door", None),
    ("start a timer", None),
    ("открой глаза", None),
    ("открой для себя новое", None),
    ("сейчас открой хром", None),
    ("oткрой ютуб", None),  # Latin "o" homoglyph
    ("открой ю​туб", None),  # zero-width space in the target
    ("open ｙoutube", None),  # fullwidth letter
    # --- not handled: unknown / unsafe targets, no verb, empty
    ("открой что-то непонятное", None),
    ("открой фотошоп", None),
    ("open nonexistent-app", None),
    ("открой file:///C:/Windows/system32", None),
    ("open javascript:alert(1)", None),
    ("открой calc.exe", None),
    ("открой", None),
    ("open", None),
    ("Луна", None),
    ("", None),
    ("   ", None),
    ("ютуб", None),
    ("привет, как дела", None),
    ("я хочу открыть ютуб", None),
    ("x " * 150 + "открой ютуб", None),
]


def test_table_is_big_enough() -> None:
    assert len(CASES) >= 40


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_phrase_table(world: World, text: str, expected: tuple[str, str] | None) -> None:
    res = world.route(text)
    if expected is None:
        assert not res.handled
        assert res.reply is None
        assert world.browser.urls == []
        assert world.launched() == []
        return
    assert res.handled
    assert res.reply is not None
    assert res.reply.provider == "router"
    kind, value = expected
    if kind == "site":
        assert world.browser.urls == [value]
        assert world.launched() == []
    else:
        assert world.launched() == [value]
        assert world.browser.urls == []


def test_actions_go_through_registry_and_are_logged(world: World) -> None:
    world.route("Луна, открой ютуб")
    records = world.log.current_path().read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    assert '"open_website"' in records[0]
    assert '"router"' in records[0]
    assert "Луна, открой ютуб" in records[0]


def test_app_after_site_no_match_is_logged_twice(world: World) -> None:
    world.route("open telegram")
    records = world.log.current_path().read_text(encoding="utf-8").splitlines()
    assert [("open_website" in r, "open_app" in r) for r in records] == [
        (True, False),
        (False, True),
    ]


@pytest.mark.parametrize(
    ("text", "reply", "lang"),
    [
        ("открой ютуб", "Открываю YouTube.", "ru"),
        ("open YouTube", "Opening YouTube.", "en"),
        ("запусти хром", "Открываю Google Chrome.", "ru"),
        ("open telegram", "Opening Telegram.", "en"),
        ("Луна, открой калькулятор", "Открываю Калькулятор.", "ru"),
        ("open habr.com", "Opening habr.com.", "en"),
        ("open github please", "Opening GitHub.", "en"),
        ("открой гитхаб", "Открываю GitHub.", "ru"),
    ],
)
def test_reply_text_and_language(world: World, text: str, reply: str, lang: str) -> None:
    res = world.route(text)
    assert res.reply is not None
    assert (res.reply.text, res.reply.lang) == (reply, lang)
    assert res.reply.actions and all(a.ok or a.data.get("reason") for a in res.reply.actions)
    assert res.reply.actions[-1].ok


def test_handled_requests_never_force_search(world: World) -> None:
    assert not world.route("открой ютуб").force_search


@pytest.mark.parametrize(
    ("text", "force"),
    [
        ("что такое черная дыра?", False),
        ("какой курс доллара?", True),
        ("курс евро", True),
        ("цена биткоина", True),
        ("какая погода в Москве", True),
        ("что нового сегодня", True),
        ("сколько сейчас времени", True),
        ("последние новости", True),
        ("bitcoin price", True),
        ("exchange rate usd", True),
        ("weather in Berlin", True),
        ("today's news", True),
        ("расскажи про курсовую работу", False),
        ("what is a pirate", False),
        ("the corporate rates", True),
        ("rating of movies", False),
        ("сейчас открой хром", False),
        ("открой хром сейчас", False),
        ("open the weather app", False),
        ("расскажи про цензуру", False),
        ("", False),
    ],
)
def test_force_search_keywords(world: World, text: str, force: bool) -> None:
    res = world.route(text)
    assert not res.handled
    assert res.force_search is force
    assert needs_fresh_facts(text) is force


def test_black_hole_question_is_plain_fallthrough(world: World) -> None:
    res = world.route("что такое черная дыра?")
    assert (res.handled, res.force_search) == (False, False)


def test_both_tools_no_match_falls_through_without_raising(tmp_path: Path) -> None:
    no_match = ToolResult("x", False, "no", {"reason": "no_match"})
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    site = fake("open_website", no_match)
    app = fake("open_app", no_match)
    registry.register(site)
    registry.register(app)
    res = Router(registry).route(Request("открой нечто", "text"))
    assert not res.handled
    assert [c[0] for c in site.calls] == [{"target": "нечто"}]
    assert [c[0] for c in app.calls] == [{"name": "нечто"}]


def test_ambiguous_asks_which_one(tmp_path: Path) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    registry.register(fake("open_website", ToolResult("x", False, "no", {"reason": "no_match"})))
    ambiguous = ToolResult(
        "open_app",
        False,
        "several",
        {"reason": "ambiguous", "candidates": ["Telegram", "Telegram Web"]},
    )
    registry.register(fake("open_app", ambiguous))
    ru = Router(registry).route(Request("открой телеграм", "text"))
    assert ru.handled and ru.reply is not None
    assert ru.reply.lang == "ru"
    assert "Telegram, Telegram Web" in ru.reply.text and ru.reply.text.endswith("?")
    en = Router(registry).route(Request("open telegram", "text"))
    assert en.reply is not None and en.reply.lang == "en"
    assert "Telegram, Telegram Web" in en.reply.text and "Which" in en.reply.text


def test_ambiguous_without_candidates_falls_through(tmp_path: Path) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    registry.register(fake("open_website", ToolResult("x", False, "no", {"reason": "no_match"})))
    registry.register(fake("open_app", ToolResult("x", False, "?", {"reason": "ambiguous"})))
    assert not Router(registry).route(Request("open telegram", "text")).handled


@pytest.mark.parametrize("reason", ["unsupported_request", "catalog_unavailable", "invalid_name"])
def test_uncertain_app_results_fall_through(tmp_path: Path, reason: str) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    registry.register(fake("open_website", ToolResult("x", False, "no", {"reason": "no_match"})))
    registry.register(fake("open_app", ToolResult("x", False, "?", {"reason": reason})))
    assert not Router(registry).route(Request("open telegram", "text")).handled


def test_unsafe_site_target_does_not_try_app(tmp_path: Path) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    registry.register(
        fake("open_website", ToolResult("x", False, "no", {"reason": "unsafe_target"}))
    )
    app = fake("open_app")
    registry.register(app)
    assert not Router(registry).route(Request("open stuff", "text")).handled
    assert app.calls == []


def test_open_failed_is_reported_not_retried(tmp_path: Path) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    registry.register(fake("open_website", ToolResult("x", False, "no", {"reason": "no_match"})))
    registry.register(fake("open_app", ToolResult("x", False, "boom", {"reason": "open_failed"})))
    res = Router(registry).route(Request("запусти хром", "text"))
    assert res.handled and res.reply is not None
    assert res.reply.text == "Не удалось открыть Хром."


def test_missing_tools_fall_through(tmp_path: Path) -> None:
    router = Router(ToolRegistry(ActionLog(tmp_path / "a.jsonl")))
    assert not router.route(Request("открой ютуб", "text")).handled


def test_tool_context_identifies_router(tmp_path: Path) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    site = fake("open_website", ToolResult("open_website", True, "ok", {"host": "x"}))
    registry.register(site)
    req = Request("Луна, открой ютуб", "voice", request_id="req-1")
    Router(registry).route(req)
    ctx = site.calls[0][1]
    assert (ctx.request_id, ctx.source, ctx.provider) == ("req-1", "voice", "router")
    assert ctx.user_text == "Луна, открой ютуб"
    assert ctx.confirmer.confirm("anything") is False


def test_routing_is_fast(world: World) -> None:
    start = time.perf_counter()
    world.route("Луна, открой ютуб")
    world.route("запусти хром пожалуйста")
    assert (time.perf_counter() - start) / 2 < 0.1


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("Луна, открой ютуб", "ютуб"),
        ("Luna open  YouTube !", "YouTube"),
        ("запусти хром пожалуйста", "хром"),
        ("Open the Calculator", "Calculator"),
        ("открой", None),
        ("закрой ютуб", None),
        ("открой ютуб?", None),
    ],
)
def test_parse_open_command(text: str, target: str | None) -> None:
    assert parse_open_command(text) == target


@pytest.mark.parametrize(
    ("text", "hint", "lang"),
    [
        ("открой ютуб", None, "ru"),
        ("open youtube", "ru", "en"),
        ("123", "en", "en"),
        ("123", None, "ru"),
        ("123", "de", "ru"),
    ],
)
def test_detect_lang(text: str, hint: str | None, lang: str) -> None:
    assert detect_lang(text, hint) == lang
