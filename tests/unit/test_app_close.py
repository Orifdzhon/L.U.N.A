"""close_app wiring: registration, [apps] settings and an LLM-driven close with fakes only."""

import pytest
from pydantic import ValidationError

from fakes import FakeAppCatalog, FakeBrowser, FakeLauncher, FakeLLM, FakeRunningApps
from luna.app import build_app
from luna.config.settings import AppsSettings, Settings
from luna.core.models import Request
from luna.llm.base import ChatResponse, ToolCall
from luna.platform.base import AppEntry, RunningApp

TELEGRAM = AppEntry("Telegram", "Telegram.TelegramDesktop")
RUNNING = RunningApp("tg", "Telegram Desktop", "C:\\t\\Telegram.exe", None, (1,), 1)


def build(tmp_path, llm, **kw):
    catalog = FakeAppCatalog([TELEGRAM])
    return build_app(
        Settings().with_api_keys({}),
        llm=llm,
        log_dir=tmp_path,
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        browser=FakeBrowser(),
        **kw,
    )


def tool_names(app):
    return [s.name for s in app.registry.schemas()]


def test_close_app_is_registered_only_with_running_apps(tmp_path):
    say = ChatResponse("ok", (), "fake", "m", "stop")
    with_running = build(tmp_path / "a", FakeLLM([say]), running=FakeRunningApps([RUNNING]))
    without = build(tmp_path / "b", FakeLLM([say]))
    try:
        assert "close_app" in tool_names(with_running)
        assert "close_app" not in tool_names(without)
    finally:
        with_running.close()
        without.close()


def test_llm_closes_an_app_end_to_end(tmp_path):
    running = FakeRunningApps([RUNNING], identity={"tg": TELEGRAM.app_id})
    call = ChatResponse(
        None, (ToolCall("c1", "close_app", {"name": "телеграм"}),), "fake", "m", "tool_calls"
    )
    done = ChatResponse("Закрыла Telegram.", (), "fake", "m", "stop")
    app = build(tmp_path, FakeLLM([call, done]), running=running)
    try:
        reply = app.orchestrator.handle(Request("закрой телеграм", "text"))
    finally:
        app.close()
    assert running.closed == ["tg"]
    assert "Telegram" in reply.text


def test_apps_settings_defaults_and_validation():
    conf = Settings().apps
    assert (conf.close_wait_s, conf.force_window_s, conf.protected_extra) == (3.0, 60.0, [])
    assert AppsSettings(protected_extra=["keepass"]).protected_extra == ["keepass"]
    with pytest.raises(ValidationError):
        AppsSettings(close_wait_s=0)
    with pytest.raises(ValidationError):
        AppsSettings(unknown=1)  # type: ignore[call-arg]
