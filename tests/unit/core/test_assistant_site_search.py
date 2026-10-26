from pathlib import Path

from fakes import FakeBrowser, FakeLLM
from luna.core.assistant import Assistant
from luna.core.models import Request
from luna.llm.base import ChatResponse, ToolCall
from luna.logs.action_log import ActionLog
from luna.tools.builtin.open_website import OpenWebsiteTool
from luna.tools.registry import ToolRegistry


def test_model_calls_open_website_with_query(tmp_path: Path) -> None:
    browser = FakeBrowser()
    call = ChatResponse(
        None,
        (ToolCall("c1", "open_website", {"target": "ютуб", "query": "lofi hip hop radio"}),),
        "fake",
        "m",
        "tool_calls",
    )
    llm = FakeLLM([call, ChatResponse("Открыл поиск", (), "fake", "m", "stop")])
    registry = ToolRegistry(ActionLog(tmp_path / "actions.jsonl"))
    registry.register(OpenWebsiteTool(browser))
    assistant = Assistant(llm, registry, now_context=lambda: "NOW")

    reply = assistant.answer(Request("открой ютуб и найди lofi hip hop radio", "voice"))

    assert reply.text == "Открыл поиск"
    assert browser.urls == ["https://www.youtube.com/results?search_query=lofi+hip+hop+radio"]
    system = llm.calls[0].messages[0].content or ""
    assert "open_website" in system and "query" in system
    assert "query" in llm.calls[0].tools[0].parameters["properties"]
