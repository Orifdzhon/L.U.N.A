import pytest

from fakes import FakeAppCatalog, FakeClock, FakeConfirmer, FakeRunningApps
from luna.config.settings import AppsSettings
from luna.core.errors import ToolError
from luna.llm.base import ToolCall
from luna.logs.action_log import ActionLog
from luna.platform.base import AppEntry, CloseOutcome, ProtectedApp, RunningApp
from luna.platform.fallback import NoRunningApps
from luna.tools.base import SafetyLevel, Tool, ToolContext
from luna.tools.builtin.close_app import CloseAppTool
from luna.tools.registry import ToolRegistry
from luna.tools.schema import validate_args

TG_ENTRY = AppEntry("Telegram", "Pkg.Telegram!App")
CATALOG = [TG_ENTRY, AppEntry("Discord", "D:\\Apps\\Discord\\Discord.exe")]
TG = RunningApp("tg-key", "Telegram Desktop", "C:\\t\\Telegram.exe", "Pkg.Telegram!App", (1,), 1)
TOOL_X = RunningApp("x-key", "Some Tool", "D:\\x\\sometool.exe", None, (2,), 1)


def ctx(request_id="r1"):
    return ToolContext(request_id, "voice", FakeConfirmer(), "закрой")


class Env:
    def __init__(self, **kw):
        self.running = FakeRunningApps([TG, TOOL_X], identity={"tg-key": TG_ENTRY.app_id}, **kw)
        self.clock = FakeClock()
        self.tool = CloseAppTool(
            self.running, FakeAppCatalog(CATALOG), conf=AppsSettings(), clock=self.clock
        )

    def run(self, name="телеграм", request_id="r1", **args):
        return self.tool.run({"name": name, **args}, ctx(request_id))


@pytest.fixture
def env():
    return Env()


def test_is_a_safe_tool_with_a_valid_schema(env):
    assert isinstance(env.tool, Tool)
    assert env.tool.safety is SafetyLevel.SAFE
    params = env.tool.parameters
    assert validate_args({"name": "x", "force": True}, params) == []
    assert validate_args({"name": "x", "pid": 4}, params)
    assert validate_args({"name": ""}, params)
    assert validate_args({"name": "x", "force": "yes"}, params)


def test_closed(env):
    r = env.run("закрой телеграм")
    assert (r.ok, r.data["reason"], r.data["app"]) == (True, "closed", "Telegram")
    assert env.running.closed == ["tg-key"]
    assert env.running.waits == [3.0]  # [apps] close_wait_s
    assert "Telegram" in r.summary


def test_prompted_is_ok_and_does_nothing_more():
    e = Env(outcomes={"tg-key": CloseOutcome.PROMPTED})
    r = e.run()
    assert (r.ok, r.data["reason"]) == (True, "prompted")
    assert e.running.terminated == []
    assert e.run(force=True, request_id="r2").data["reason"] == "force_not_offered"


def test_not_running_by_outcome():
    r = Env(outcomes={"tg-key": CloseOutcome.NOT_RUNNING}).run()
    assert (r.ok, r.data["reason"]) == (False, "not_running")


def test_installed_but_not_running(env):
    r = env.run("discord")
    assert (r.ok, r.data["reason"]) == (False, "not_running")
    assert env.running.closed == []


def test_no_match(env):
    r = env.run("фотошоп")
    assert (r.ok, r.data["reason"]) == (False, "no_match")
    assert env.running.closed == []


def test_unsupported_request(env):
    assert env.run("телеграм и дискорд").data["reason"] == "unsupported_request"


def test_ambiguous():
    e = Env()
    e.running.apps = [
        RunningApp("a", "Visual Tool One", "D:\\a.exe", None, (1,), 1),
        RunningApp("b", "Visual Tool Two", "D:\\b.exe", None, (2,), 1),
    ]
    r = e.run("visual tool")
    assert (r.ok, r.data["reason"]) == (False, "ambiguous")
    assert r.data["candidates"] == ["Visual Tool One", "Visual Tool Two"]
    assert e.running.closed == []


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_invalid_name(env, bad):
    assert env.tool.run({"name": bad}, ctx()).data["reason"] == "invalid_name"


def test_unsupported_platform():
    tool = CloseAppTool(NoRunningApps(), FakeAppCatalog(CATALOG))
    assert tool.run({"name": "телеграм"}, ctx()).data["reason"] == "unsupported_platform"


def test_close_failed_and_protected():
    r = Env(close_error=ToolError("boom")).run()
    assert (r.ok, r.data["reason"]) == (False, "close_failed")
    r = Env(close_error=ProtectedApp("no")).run()
    assert (r.ok, r.data["reason"]) == (False, "protected")


def test_listing_failure_is_close_failed(env):
    def boom():
        raise ToolError("enum failed")

    env.running.list_running = boom
    assert env.run().data["reason"] == "close_failed"


# --- the force rule -------------------------------------------------------------------------


def hung():
    return Env(outcomes={"tg-key": CloseOutcome.NOT_RESPONDING})


def test_not_responding_offers_force_and_a_later_request_may_force():
    e = hung()
    r = e.run(request_id="r1")
    assert (r.ok, r.data["reason"]) == (False, "not_responding")
    assert "force-close" in r.summary and "unsaved" in r.summary
    e.clock.advance(20)
    forced = e.run(force=True, request_id="r2")
    assert (forced.ok, forced.data["reason"], forced.data["forced"]) == (True, "closed", True)
    assert e.running.terminated == ["tg-key"]
    assert e.running.closed == ["tg-key"]  # only the first, graceful attempt


def test_hidden_tray_app_offers_force_too():
    e = Env(outcomes={"tg-key": CloseOutcome.HIDDEN})
    r = e.run()
    assert (r.ok, r.data["reason"]) == (True, "hidden")
    assert "tray" in r.summary
    assert e.run(force=True, request_id="r2").data["forced"] is True


def test_force_without_an_offer_is_refused_and_closes_gracefully(env):
    r = env.run(force=True)
    assert (r.ok, r.data["reason"]) == (False, "force_not_offered")
    assert r.data["outcome"] == "closed"
    assert env.running.terminated == []
    assert env.running.closed == ["tg-key"]


def test_force_in_the_same_request_is_refused():
    e = hung()
    e.run(request_id="r1")
    r = e.run(force=True, request_id="r1")
    assert r.data["reason"] == "force_not_offered"
    assert e.running.terminated == []


def test_force_after_the_window_is_refused():
    e = hung()
    e.run(request_id="r1")
    e.clock.advance(61)
    assert e.run(force=True, request_id="r2").data["reason"] == "force_not_offered"
    assert e.running.terminated == []


def test_force_is_accepted_at_exactly_the_window_edge():
    e = hung()
    e.run(request_id="r1")
    e.clock.advance(60)
    assert e.run(force=True, request_id="r2").data["forced"] is True


def test_force_for_another_app_is_refused():
    e = hung()
    e.run(request_id="r1")
    r = e.run("sometool", force=True, request_id="r2")
    assert r.data["reason"] == "force_not_offered"
    assert e.running.terminated == []
    assert e.running.closed == ["tg-key", "x-key"]


def test_force_for_a_restarted_app_is_refused():
    e = hung()
    e.run(request_id="r1")
    restarted = RunningApp("tg-key2", TG.name, TG.exe_path, TG.app_id, (9,), 1)
    e.running.apps = [restarted, TOOL_X]
    e.running.identity = {"tg-key2": TG_ENTRY.app_id}
    assert e.run(force=True, request_id="r2").data["reason"] == "force_not_offered"
    assert e.running.terminated == []


def test_offer_is_single_use():
    e = hung()
    e.run(request_id="r1")
    assert e.run(force=True, request_id="r2").data["forced"] is True
    assert e.run(force=True, request_id="r3").data["reason"] == "force_not_offered"
    assert len(e.running.terminated) == 1


def test_any_other_close_call_clears_the_offer():
    e = hung()
    e.run(request_id="r1")
    e.run("sometool", request_id="r2")  # unrelated close in between
    assert e.run(force=True, request_id="r3").data["reason"] == "force_not_offered"
    assert e.running.terminated == []


def test_force_termination_errors():
    e = hung()
    e.run(request_id="r1")
    e.running.terminate_error = ProtectedApp("no")
    assert e.run(force=True, request_id="r2").data["reason"] == "protected"
    e.run(request_id="r3")
    e.running.terminate_error = ToolError("gone")
    assert e.run(force=True, request_id="r4").data["reason"] == "close_failed"


# --- through the registry (action log) ------------------------------------------------------


def test_action_log_records_args_and_display_name_only(tmp_path):
    e = Env()
    log = ActionLog(tmp_path / "actions.jsonl")
    registry = ToolRegistry(log)
    registry.register(e.tool)
    result = registry.execute(ToolCall("c1", "close_app", {"name": "закрой телеграм"}), ctx())
    assert result.ok
    lines = log.current_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "close_app" in lines[0] and "Telegram" in lines[0]
    assert "tg-key" not in lines[0]  # the opaque identity never leaves the tool
