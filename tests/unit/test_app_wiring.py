# ruff: noqa: RUF001
import json
import logging
import threading

import pytest

from fakes import FakeAppCatalog, FakeBrowser, FakeLauncher, FakeLLM, FakeSearch
from luna.app import App, build_app
from luna.config.settings import Settings
from luna.core.models import Request
from luna.llm.base import ChatResponse, ToolCall
from luna.logs.redact import clear_secrets, register_secrets
from luna.platform.base import AppEntry
from luna.search.base import SearchResult

SECRET = "sk-test-SECRET-1234567890"
TELEGRAM = AppEntry("Telegram", "Telegram.TelegramDesktop")


def settings(env: dict[str, str] | None = None) -> Settings:
    s = Settings().with_api_keys(env or {})
    s.search.fetch_pages = 0  # no page downloads in unit tests
    return s


def say(text: str) -> ChatResponse:
    return ChatResponse(text, (), "fake", "m", "stop")


def call(name: str, **args: object) -> ChatResponse:
    return ChatResponse(None, (ToolCall("c1", name, dict(args)),), "fake", "m", "tool_calls")


@pytest.fixture(autouse=True)
def _secrets():
    clear_secrets()
    yield
    clear_secrets()


@pytest.fixture
def made(tmp_path):
    apps: list[App] = []

    def factory(llm=None, env=None, **kw) -> App:
        catalog = kw.setdefault("catalog", FakeAppCatalog([TELEGRAM]))
        kw.setdefault("launcher", FakeLauncher(catalog))
        kw.setdefault("browser", FakeBrowser())
        kw.setdefault("search", FakeSearch([SearchResult("Rate", "https://cbr.example/r", "90")]))
        app = build_app(settings(env), llm=llm, log_dir=tmp_path / "logs", **kw)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()


def test_four_example_phrases(made, tmp_path):
    llm = FakeLLM(
        [
            call("web_search", query="курс доллара"),
            say("Курс доллара около 90 (cbr.example)."),
            say("Чёрная дыра — область пространства с очень сильной гравитацией."),
        ]
    )
    browser = FakeBrowser()
    catalog = FakeAppCatalog([TELEGRAM])
    launcher = FakeLauncher(catalog)
    app = made(llm, browser=browser, catalog=catalog, launcher=launcher)
    handle = app.orchestrator.handle

    handle(Request("открой ютуб", "text"))
    assert any("youtube" in u for u in browser.urls)
    handle(Request("open telegram", "text"))
    assert [a.name for a in launcher.launched] == ["Telegram"]
    rate = handle(Request("какой курс доллара?", "text"))
    assert "90" in rate.text
    hole = handle(Request("что такое черная дыра?", "text"))
    assert "гравитац" in hole.text

    log = next((tmp_path / "logs").glob("actions-*.jsonl"))
    tools = [json.loads(x)["tool"] for x in log.read_text(encoding="utf-8").splitlines()]
    # open telegram: the Router tries open_website first (refused), then open_app.
    assert tools == ["open_website", "open_website", "open_app", "web_search"]


def test_providers_skipped_without_keys(made):
    app = made(env={})
    assert app.active_providers == ["local"]
    assert set(app.missing_keys) == {"gemini", "groq"}
    text = "\n".join(app.startup_lines())
    assert "GEMINI_API_KEY" in text
    assert "local" in text


def test_providers_with_keys_in_chain_order(made):
    app = made(env={"GEMINI_API_KEY": SECRET, "GROQ_API_KEY": SECRET + "2"})
    assert app.active_providers == ["gemini", "groq", "local"]
    assert app.missing_keys == {}
    assert SECRET not in "\n".join(app.startup_lines())


def test_no_provider_still_starts_and_router_works(tmp_path):
    s = settings()
    s.llm.chain = ["gemini"]
    browser = FakeBrowser()
    app = build_app(s, browser=browser, platform="linux", log_dir=tmp_path)
    try:
        assert not app.llm_available
        assert "none" in app.startup_lines()[0]
        assert app.orchestrator.handle(Request("открой ютуб", "text")).provider == "router"
        assert browser.urls
        answer = app.orchestrator.handle(Request("что такое черная дыра?", "text"))
        assert answer.text  # an apology, no stack trace
        assert answer.provider is None
    finally:
        app.close()


def test_non_windows_disables_app_launching(tmp_path):
    app = build_app(settings(), browser=FakeBrowser(), platform="linux", log_dir=tmp_path)
    try:
        assert "open_app" not in app.tool_names
        assert any("Windows" in w for w in app.warnings)
        assert {"open_website", "web_search", "get_datetime"} <= set(app.tool_names)
    finally:
        app.close()


def test_tools_registered(made):
    assert made().tool_names == ["get_datetime", "open_app", "open_website", "web_search"]


def test_catalog_warmed_in_background_without_blocking(tmp_path):
    release = threading.Event()

    class SlowCatalog(FakeAppCatalog):
        def list_apps(self):
            release.wait(5)
            return super().list_apps()

    catalog = SlowCatalog()
    app = build_app(
        settings(),
        llm=FakeLLM([]),
        browser=FakeBrowser(),
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        search=FakeSearch(),
        log_dir=tmp_path,
    )  # returned although the scan is still blocked
    thread = app._catalog_thread
    assert thread is not None
    assert thread.is_alive()
    release.set()
    app.close()
    assert not thread.is_alive()


def test_close_is_idempotent_and_leaks_no_threads(made):
    before = {t.ident for t in threading.enumerate()}
    app = made(FakeLLM([say("hi")]))
    app.orchestrator.handle(Request("привет", "text"))
    app.close()
    app.close()
    leaked = [
        t for t in threading.enumerate() if t.ident not in before and t.name.startswith("luna-")
    ]
    assert leaked == []


def test_secrets_not_in_logs_or_startup(made, tmp_path, caplog):
    register_secrets([SECRET])
    caplog.set_level(logging.DEBUG)
    app = made(env={"GEMINI_API_KEY": SECRET})
    app.orchestrator.handle(Request("открой ютуб", "text"))
    out = "\n".join(app.startup_lines()) + caplog.text + repr(app)
    for path in (tmp_path / "logs").glob("*"):
        out += path.read_text(encoding="utf-8")
    assert SECRET not in out
