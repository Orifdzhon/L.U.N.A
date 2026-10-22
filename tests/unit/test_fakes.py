import pytest

from fakes import (
    FakeAppCatalog,
    FakeBrowser,
    FakeClock,
    FakeConfirmer,
    FakeLauncher,
    FakeLLM,
    FakeSearch,
    FakeTool,
    ScriptExhausted,
)
from luna.core.errors import ProviderUnavailable, RateLimited, ToolError
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
    assert [(c.messages, c.tools, c.timeout_s) for c in llm.calls] == [
        (msgs, [schema], 1),
        (msgs, [], 1),
    ]


def test_fake_llm_records_copies():
    llm = FakeLLM([_resp("a")])
    msgs = [Message(role="user", content="hi")]
    llm.chat(msgs, timeout_s=2.5)
    msgs.append(Message(role="user", content="later"))
    assert len(llm.calls[0].messages) == 1
    assert llm.calls[0].timeout_s == 2.5


def test_fake_llm_raises_scripted_errors():
    llm = FakeLLM([RateLimited(retry_after_s=3), ProviderUnavailable("down"), _resp("ok")])
    with pytest.raises(RateLimited) as exc:
        llm.chat([], timeout_s=1)
    assert exc.value.retry_after_s == 3
    with pytest.raises(ProviderUnavailable):
        llm.chat([], timeout_s=1)
    assert llm.chat([], timeout_s=1).text == "ok"


def test_fake_llm_exhausted_script_fails_loudly():
    with pytest.raises(ScriptExhausted):
        FakeLLM([]).chat([], timeout_s=1)
    assert not issubclass(ScriptExhausted, Exception)


def test_fake_search_records_queries_and_limits_results():
    results = [SearchResult(title=str(i), url=f"https://x/{i}", snippet="s") for i in range(3)]
    search = FakeSearch(results)
    assert search.search("q", lang="en", max_results=2) == results[:2]
    assert search.queries == ["q"]
    assert search.calls[0].lang == "en"
    assert search.calls[0].max_results == 2
    failing = FakeSearch(error=ProviderUnavailable("down"))
    with pytest.raises(ProviderUnavailable):
        failing.search("q", lang="en")


def test_fake_catalog_launcher_browser():
    app = AppEntry(name="Calculator", app_id="calc", aliases=("calc",))
    catalog = FakeAppCatalog([app])
    assert catalog.list_apps() == (app,)
    catalog.refresh()
    assert catalog.refresh_count == 1
    launcher = FakeLauncher()
    launcher.launch(app)
    assert launcher.launched == [app]
    browser = FakeBrowser()
    browser.open_url("https://example.com")
    assert browser.urls == ["https://example.com"]
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ms-settings:", "example.com"):
        with pytest.raises(ValueError):
            browser.open_url(bad)
    assert browser.urls == ["https://example.com"]


def test_fake_launcher_rejects_apps_outside_catalog():
    app = AppEntry(name="Calculator", app_id="calc")
    launcher = FakeLauncher(FakeAppCatalog([app]))
    launcher.launch(app)
    with pytest.raises(ToolError):
        launcher.launch(AppEntry(name="Evil", app_id="evil"))
    assert launcher.launched == [app]


def test_fake_confirmer_records_prompts():
    confirmer = FakeConfirmer(answer=False)
    assert confirmer.confirm("sure?") is False
    assert confirmer.prompts == ["sure?"]


def test_fake_clock_advances_manually():
    from luna.core.clock import Clock

    _: Clock = FakeClock()
    clock = FakeClock(10.0)
    assert clock() == 10.0
    clock.advance(2.5)
    assert clock() == 12.5
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


def test_fake_tool_stores_args_copy_and_can_raise():
    ctx = ToolContext(request_id="r", source="text", confirmer=FakeConfirmer(), user_text="")
    tool = FakeTool(error=ToolError("boom"))
    args = {"a": [1]}
    with pytest.raises(ToolError):
        tool.run(args, ctx)
    args["a"].append(2)
    assert tool.calls[0][0] == {"a": [1]}


def test_message_invariants():
    with pytest.raises(ValueError):
        Message(role="tool", content="x")
    with pytest.raises(ValueError):
        Message(role="tool", content=None, tool_call_id="1")
    with pytest.raises(ValueError):
        Message(role="user", content="x", tool_call_id="1")
    with pytest.raises(ValueError):
        Message(role="user", tool_calls=(ToolCall(id="1", name="n", arguments={}),))
    assert Message(role="tool", content="r", tool_call_id="1").tool_call_id == "1"
    call = ToolCall(id="1", name="n", arguments={})
    assert Message(role="assistant", tool_calls=(call,)).tool_calls == (call,)


def test_chat_response_optional_fields():
    r = ChatResponse(text="x", tool_calls=(), provider="p", model="m")
    assert r.finish_reason is None
    assert r.usage is None


def test_model_defaults():
    assert ToolResult(tool="t", ok=True, summary="s").untrusted is False
    assert {s.name for s in SafetyLevel} == {"SAFE", "SENSITIVE", "DANGEROUS"}
    assert ToolCall(id="1", name="n", arguments={}).arguments == {}
