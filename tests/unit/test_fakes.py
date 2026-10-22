import pytest
from tests.fakes import (
    FakeAppCatalog,
    FakeBrowser,
    FakeClock,
    FakeConfirmer,
    FakeLauncher,
    FakeLLM,
    FakeSearch,
    FakeTool,
)

from luna.core.errors import ProviderUnavailable, RateLimited
from luna.llm.base import ChatResponse, LLMProvider, Message, ToolCall, ToolSchema
from luna.platform.base import (
    AppCatalog,
    AppEntry,
    AppLauncher,
    Autostart,
    BrowserOpener,
    HotkeyListener,
)
from luna.search.base import SearchProvider, SearchResult
from luna.tools.base import Confirmer, SafetyLevel, Tool, ToolContext, ToolResult


def _resp(text: str) -> ChatResponse:
    return ChatResponse(text=text, tool_calls=(), provider="fake", model="m")


def test_fakes_satisfy_protocols():
    assert isinstance(FakeLLM([]), LLMProvider)
    assert isinstance(FakeSearch(), SearchProvider)
    assert isinstance(FakeAppCatalog(), AppCatalog)
    assert isinstance(FakeLauncher(), AppLauncher)
    assert isinstance(FakeBrowser(), BrowserOpener)
    assert isinstance(FakeConfirmer(), Confirmer)
    assert isinstance(FakeTool(), Tool)


def test_hotkey_and_autostart_protocols_are_importable():
    assert HotkeyListener is not None
    assert Autostart is not None


def test_fake_llm_replays_script_and_records_calls():
    llm = FakeLLM([_resp("a"), _resp("b")])
    msgs = [Message(role="user", content="hi")]
    schema = ToolSchema(name="t", description="d", parameters={})
    assert llm.chat(msgs, [schema], timeout_s=1).text == "a"
    assert llm.chat(msgs, timeout_s=1).text == "b"
    assert llm.calls == [(msgs, [schema]), (msgs, [])]


def test_fake_llm_raises_scripted_errors():
    llm = FakeLLM([RateLimited(retry_after_s=3), ProviderUnavailable("down"), _resp("ok")])
    with pytest.raises(RateLimited) as exc:
        llm.chat([], timeout_s=1)
    assert exc.value.retry_after_s == 3
    with pytest.raises(ProviderUnavailable):
        llm.chat([], timeout_s=1)
    assert llm.chat([], timeout_s=1).text == "ok"


def test_fake_llm_exhausted_script_fails_loudly():
    with pytest.raises(AssertionError):
        FakeLLM([]).chat([], timeout_s=1)


def test_fake_search_records_queries_and_limits_results():
    results = [SearchResult(title=str(i), url=f"https://x/{i}", snippet="s") for i in range(3)]
    search = FakeSearch(results)
    assert search.search("q", lang="en", max_results=2) == results[:2]
    assert search.queries == ["q"]
    failing = FakeSearch(error=ProviderUnavailable("down"))
    with pytest.raises(ProviderUnavailable):
        failing.search("q", lang="en")


def test_fake_catalog_launcher_browser():
    app = AppEntry(name="Calculator", app_id="calc", aliases=("calc",))
    catalog = FakeAppCatalog([app])
    assert catalog.list_apps() == [app]
    catalog.refresh()
    assert catalog.refresh_count == 1
    launcher = FakeLauncher()
    launcher.launch(app)
    assert launcher.launched == [app]
    browser = FakeBrowser()
    browser.open_url("https://example.com")
    assert browser.urls == ["https://example.com"]


def test_fake_confirmer_records_prompts():
    confirmer = FakeConfirmer(answer=False)
    assert confirmer.confirm("sure?") is False
    assert confirmer.prompts == ["sure?"]


def test_fake_clock_advances_manually():
    clock = FakeClock(10.0)
    assert clock.now() == 10.0
    clock.advance(2.5)
    assert clock.monotonic() == 12.5
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_fake_tool_records_calls_and_returns_result():
    tool = FakeTool(name="t")
    ctx = ToolContext(
        request_id="r", source="text", confirmer=FakeConfirmer(), user_text="hi", provider=None
    )
    result = tool.run({"a": 1}, ctx)
    assert result == ToolResult(tool="t", ok=True, summary="ok")
    assert tool.calls == [({"a": 1}, ctx)]


def test_model_defaults():
    assert ToolResult(tool="t", ok=True, summary="s").untrusted is False
    assert {s.name for s in SafetyLevel} == {"SAFE", "SENSITIVE", "DANGEROUS"}
    assert ToolCall(id="1", name="n", arguments={}).arguments == {}
