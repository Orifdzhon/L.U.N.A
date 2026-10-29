"""supports_tools = false: the chain calls that provider without tools, logging once (D16)."""

import logging

from fakes import FakeClock, FakeLLM
from luna.llm.base import ChatResponse, Message, ToolSchema
from luna.llm.chain import LLMChain

MSGS = [Message(role="user", content="hi")]
TOOLS = [ToolSchema("ping", "d", {"type": "object", "properties": {}})]


def ok(name):
    return ChatResponse(text="ok", tool_calls=(), provider=name, model="m")


def test_provider_without_tool_support_gets_no_tools(caplog):
    a = FakeLLM([ok("a"), ok("a")], name="a")
    chain = LLMChain([a], FakeClock(), no_tools={"a"})
    with caplog.at_level(logging.INFO, logger="luna.llm.chain"):
        chain.chat(MSGS, TOOLS, timeout_s=5)
        chain.chat(MSGS, TOOLS, timeout_s=5)
    assert [c.tools for c in a.calls] == [[], []]
    assert sum("supports_tools" in r.getMessage() for r in caplog.records) == 1


def test_other_providers_are_unchanged():
    b = FakeLLM([ok("b")], name="b")
    LLMChain([b], FakeClock(), no_tools={"a"}).chat(MSGS, TOOLS, timeout_s=5)
    assert [t.name for t in b.calls[0].tools] == ["ping"]


def test_no_log_when_no_tools_were_offered(caplog):
    a = FakeLLM([ok("a")], name="a")
    with caplog.at_level(logging.INFO, logger="luna.llm.chain"):
        LLMChain([a], FakeClock(), no_tools={"a"}).chat(MSGS, timeout_s=5)
    assert not caplog.records
