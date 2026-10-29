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
    assert app.active_providers == ["groq", "gemini", "local"]
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
    assert made().tool_names == [
        "find_file",
        "get_datetime",
        "open_app",
        "open_file",
        "open_website",
        "web_search",
    ]


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


def test_system_prompt_contains_current_datetime(made):
    llm = FakeLLM([say("ok")])
    app = made(llm)
    app.orchestrator.handle(Request("что такое черная дыра?", "text"))
    system = llm.calls[0].messages[0]
    assert system.role == "system"
    assert "Current local date and time: " in system.content


def test_time_question_is_answered_by_router_without_llm(made):
    llm = FakeLLM([])
    app = made(llm)
    reply = app.orchestrator.handle(Request("Луна, сколько сейчас времени?", "text"))
    assert reply.provider == "router"
    assert reply.text.startswith("Сейчас ")
    assert llm.calls == []


def test_assistant_timeout_follows_settings():
    from luna.app import assistant_timeout

    s = Settings()
    assert assistant_timeout(s) == 17.0  # request 20 - margin 3, llm 20
    s.llm.timeout_s = 8.0
    assert assistant_timeout(s) == 8.0
    s.app.request_timeout_s = 6.0
    assert assistant_timeout(s) == 3.0


def test_request_timeout_must_exceed_margin():
    from pydantic import ValidationError

    from luna.config.settings import AppSettings

    with pytest.raises(ValidationError):
        AppSettings(request_timeout_s=3.0)
    assert AppSettings(request_timeout_s=3.5).request_timeout_s == 3.5


def test_failed_build_closes_created_providers(tmp_path, monkeypatch):
    closed = []

    class P:
        name = "local"
        model = "m"

        def close(self):
            closed.append(1)

    monkeypatch.setattr("luna.app._build_providers", lambda s: ([P()], {}))
    monkeypatch.setattr("luna.app.GetDatetimeTool", lambda: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        build_app(settings(), browser=FakeBrowser(), platform="linux", log_dir=tmp_path)
    assert closed == [1]
