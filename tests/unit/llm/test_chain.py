from __future__ import annotations

import time
from collections.abc import Sequence

import pytest

from fakes import FakeClock, FakeLLM, ScriptExhausted
from luna.core.errors import (
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
)
from luna.core.events import EventBus, ProviderSwitched
from luna.llm.base import ChatResponse, LLMProvider, Message, ToolSchema
from luna.llm.chain import LLMChain

MSGS = [Message(role="user", content="hi")]


def ok(provider: str) -> ChatResponse:
    return ChatResponse(text="ok", tool_calls=(), provider=provider, model="m")


def llm(name: str, *script: ChatResponse | Exception) -> FakeLLM:
    return FakeLLM(script, name=name)


def chat(chain: LLMChain, timeout_s: float = 20.0) -> ChatResponse:
    return chain.chat(MSGS, timeout_s=timeout_s)


def test_is_an_llm_provider() -> None:
    assert isinstance(LLMChain([llm("a")], FakeClock()), LLMProvider)


def test_rejects_empty_and_duplicate_providers() -> None:
    with pytest.raises(ValueError):
        LLMChain([], FakeClock())
    with pytest.raises(ValueError):
        LLMChain([llm("a"), llm("a")], FakeClock())


def test_first_provider_answers() -> None:
    a, b = llm("a", ok("a")), llm("b")
    assert chat(LLMChain([a, b], FakeClock())).provider == "a"
    assert b.calls == []


def test_rate_limited_falls_back_and_respects_retry_after() -> None:
    clock = FakeClock()
    a = llm("a", RateLimited(retry_after_s=30), ok("a"))
    b = llm("b", ok("b"), ok("b"), ok("b"))
    chain = LLMChain([a, b], clock)

    assert chat(chain).provider == "b"
    assert len(a.calls) == 1
    clock.advance(29)
    assert chat(chain).provider == "b"
    assert len(a.calls) == 1  # still cooling down
    clock.advance(1.1)
    assert chat(chain).provider == "a"
    assert len(a.calls) == 2


def test_default_cooldown_and_retry_after_cap() -> None:
    clock = FakeClock()
    a = llm("a", RateLimited(), RateLimited(retry_after_s=100000), ok("a"))
    b = llm("b", ok("b"), ok("b"), ok("b"), ok("b"), ok("b"))
    chain = LLMChain([a, b], clock)
    chat(chain)
    clock.advance(59)
    chat(chain)
    assert len(a.calls) == 1
    clock.advance(2)  # past the 60 s default
    chat(chain)  # a fails again with a huge Retry-After -> capped at 900 s
    assert len(a.calls) == 2
    clock.advance(899)
    chat(chain)
    assert len(a.calls) == 2
    clock.advance(2)
    assert chat(chain).provider == "a"


def test_backoff_doubles_caps_and_resets_after_success() -> None:
    clock = FakeClock()
    cooldowns = (60, 120, 240, 480, 900, 900)
    fails = [ProviderUnavailable("down") for _ in cooldowns]
    a = llm("a", *fails, ok("a"), ProviderUnavailable("down"), ok("a"))
    b = llm("b", *[ok("b")] * 30)
    chain = LLMChain([a, b], clock)

    for expected in cooldowns:
        calls = len(a.calls)
        chat(chain)
        assert len(a.calls) == calls + 1
        clock.advance(expected - 1)
        chat(chain)
        assert len(a.calls) == calls + 1  # still cooling
        clock.advance(1.1)
    assert chat(chain).provider == "a"  # success resets the backoff
    assert chat(chain).provider == "b"  # a fails once more (scripted)
    clock.advance(60.1)  # first-failure cooldown again, not 1800
    assert chat(chain).provider == "a"


def test_all_on_cooldown_raises_immediately_without_sleeping() -> None:
    clock = FakeClock()
    a = llm("a", RateLimited(retry_after_s=30))
    b = llm("b", ProviderUnavailable("down"))
    chain = LLMChain([a, b], clock)
    with pytest.raises(ProviderUnavailable):
        chat(chain)
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable) as info:
        chat(chain)
    assert time.monotonic() - started < 0.5
    assert len(a.calls) == 1 and len(b.calls) == 1
    assert "a: cooldown" in str(info.value) and "b: cooldown" in str(info.value)


def test_summary_has_no_exception_messages() -> None:
    a = llm("a", RateLimited("key sk-SECRET123 rejected"))
    b = llm("b", ProviderUnavailable("Bearer sk-SECRET123"))
    with pytest.raises(ProviderUnavailable) as info:
        chat(LLMChain([a, b], FakeClock()))
    assert "SECRET" not in str(info.value)
    assert "a: RateLimited" in str(info.value)


def test_request_error_is_retried_on_one_more_provider_only() -> None:
    a = llm("a", ProviderRequestError("bad"))
    b = llm("b", ProviderRequestError("bad too"))
    c = llm("c", ok("c"))
    chain = LLMChain([a, b, c], FakeClock())
    with pytest.raises(ProviderRequestError, match="bad too"):
        chat(chain)
    assert c.calls == []


def test_request_error_does_not_cool_the_provider_down() -> None:
    a = llm("a", ProviderRequestError("bad"), ok("a"))
    b = llm("b", ok("b"))
    chain = LLMChain([a, b], FakeClock())
    assert chat(chain).provider == "b"
    assert chat(chain).provider == "a"


def test_gemini_400_falls_back_to_groq() -> None:
    gemini = llm("gemini", ProviderRequestError("Function call is missing a thought_signature"))
    groq = llm("groq", ok("groq"))
    bus = EventBus()
    with bus.subscribe() as sub:
        assert chat(LLMChain([gemini, groq], FakeClock(), bus)).provider == "groq"
        event = sub.queue.get_nowait()
    assert isinstance(event, ProviderSwitched)
    assert (event.from_provider, event.to_provider) == (None, "groq")


def test_request_error_with_nobody_else_raises_request_error() -> None:
    a = llm("a", ProviderRequestError("bad"))
    b = llm("b", ProviderUnavailable("down"))
    with pytest.raises(ProviderRequestError) as info:
        chat(LLMChain([a, b], FakeClock()))
    assert isinstance(info.value.__cause__, ProviderError)


def test_provider_switched_published_only_on_change() -> None:
    clock = FakeClock()
    a = llm("a", RateLimited(retry_after_s=30), ok("a"))
    b = llm("b", ok("b"), ok("b"))
    bus = EventBus()
    chain = LLMChain([a, b], clock, bus)
    with bus.subscribe() as sub:
        chat(chain)  # a fails -> b answers
        chat(chain)  # b again: no event
        clock.advance(31)
        chat(chain)  # back to a
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
    assert [(e.from_provider, e.to_provider) for e in events] == [(None, "b"), ("b", "a")]
    assert "a: RateLimited" in events[0].reason


def test_no_event_when_primary_answers_first() -> None:
    bus = EventBus()
    with bus.subscribe() as sub:
        chat(LLMChain([llm("a", ok("a"))], FakeClock(), bus))
        assert sub.queue.empty()


def test_remaining_timeout_is_passed_and_deadline_enforced() -> None:
    clock = FakeClock()

    class SlowFail:
        name = "slow"

        def chat(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolSchema] = (),
            *,
            timeout_s: float,
        ) -> ChatResponse:
            clock.advance(timeout_s)
            raise ProviderUnavailable("timeout")

    b = llm("b", ok("b"))
    chain = LLMChain([llm("a", ProviderUnavailable("x")), SlowFail(), b], clock)
    with pytest.raises(ProviderUnavailable, match="deadline"):
        chat(chain, timeout_s=8.0)
    assert b.calls == []

    c = llm("c", ok("c"))
    chat(LLMChain([c], FakeClock()), timeout_s=7.5)
    assert c.calls[0].timeout_s == 7.5


def test_script_exhausted_is_not_swallowed() -> None:
    with pytest.raises(ScriptExhausted):
        chat(LLMChain([llm("a"), llm("b", ok("b"))], FakeClock()))
