from __future__ import annotations

import time
from collections.abc import Sequence

import pytest

from fakes import FakeClock, FakeLLM, ScriptExhausted
from luna.core.errors import (
    ConfigError,
    EmptyResponse,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
    ToolUseRejected,
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


def test_exclude_skips_provider_for_one_call_only() -> None:
    a, b = llm("a", ok("a")), llm("b", ok("b"))
    chain = LLMChain([a, b], FakeClock())
    assert chain.chat(MSGS, timeout_s=5, exclude={"a"}).provider == "b"
    assert a.calls == []
    a._script.append(ok("a"))
    assert chat(chain).provider == "a"  # not on cooldown afterwards


def test_exclude_everything_raises_unavailable() -> None:
    chain = LLMChain([llm("a", ok("a"))], FakeClock())
    with pytest.raises(ProviderUnavailable):
        chain.chat(MSGS, timeout_s=5, exclude={"a"})


def test_rejects_empty_and_duplicate_providers() -> None:
    with pytest.raises(ConfigError):
        LLMChain([], FakeClock())
    with pytest.raises(ConfigError):
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


def _no_sleep(*_: object) -> None:
    raise AssertionError("LLMChain must never sleep")


def test_all_on_cooldown_raises_immediately_without_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    a = llm("a", RateLimited(retry_after_s=30))
    b = llm("b", ProviderUnavailable("down"))
    chain = LLMChain([a, b], clock)
    with pytest.raises(ProviderUnavailable):
        chat(chain)
    monkeypatch.setattr(time, "sleep", _no_sleep)
    with pytest.raises(ProviderUnavailable) as info:
        chat(chain)
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
    with pytest.raises(ProviderRequestError) as info:
        chat(chain)
    assert "bad too" not in str(info.value)
    assert "a: ProviderRequestError; b: ProviderRequestError" in str(info.value)
    assert isinstance(info.value.__cause__, ProviderRequestError)
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


def test_region_blocked_gemini_then_groq_error_still_reaches_local() -> None:
    gemini = llm("gemini", ProviderUnavailable("gemini: not available in this region"))
    groq = llm("groq", ProviderRequestError("bad"))
    local = llm("local", ok("local"))
    assert chat(LLMChain([gemini, groq, local], FakeClock())).provider == "local"


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


def test_demotion_after_two_request_errors_followed_by_success() -> None:
    clock = FakeClock()
    bad = ProviderRequestError("thought_signature")
    gemini = llm("gemini", bad, ProviderRequestError("again"), ok("gemini"), ok("gemini"))
    groq = llm("groq", *[ok("groq")] * 5)
    chain = LLMChain([gemini, groq], clock)
    chat(chain)
    chat(chain)
    assert len(gemini.calls) == 2
    chat(chain)  # the 2nd tool call of the turn: Gemini is skipped
    assert len(gemini.calls) == 2
    clock.advance(59)
    chat(chain)
    assert len(gemini.calls) == 2
    clock.advance(1.1)
    assert chat(chain).provider == "gemini"  # flat 60 s, then it is probed again


def test_success_resets_request_failure_counter() -> None:
    gemini = llm(
        "gemini", ProviderRequestError("x"), ok("gemini"), ProviderRequestError("x"), ok("gemini")
    )
    groq = llm("groq", ok("groq"), ok("groq"))
    chain = LLMChain([gemini, groq], FakeClock())
    assert [chat(chain).provider for _ in range(4)] == ["groq", "gemini", "groq", "gemini"]


def test_request_error_everywhere_demotes_nobody() -> None:
    a = llm("a", *[ProviderRequestError("x")] * 4)
    b = llm("b", *[ProviderRequestError("y")] * 4)
    chain = LLMChain([a, b], FakeClock())
    for _ in range(4):
        with pytest.raises(ProviderRequestError):
            chat(chain)
    assert len(a.calls) == 4 and len(b.calls) == 4


def test_non_positive_timeout_makes_no_provider_call() -> None:
    a = llm("a", ok("a"))
    with pytest.raises(ProviderUnavailable, match="deadline"):
        chat(LLMChain([a], FakeClock()), timeout_s=0)
    assert a.calls == []


def test_deadline_after_request_error_raises_request_error() -> None:
    clock = FakeClock()

    class Slow:
        name = "slow"

        def chat(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolSchema] = (),
            *,
            timeout_s: float,
        ) -> ChatResponse:
            clock.advance(timeout_s)
            raise ProviderRequestError("bad")

    c = llm("c", ok("c"))
    with pytest.raises(ProviderRequestError, match="deadline"):
        chat(LLMChain([Slow(), c], clock), timeout_s=5.0)
    assert c.calls == []


def test_rate_limited_after_request_error_falls_through() -> None:
    a = llm("a", ProviderRequestError("bad"))
    b = llm("b", RateLimited(retry_after_s=10))
    c = llm("c", ok("c"))
    assert chat(LLMChain([a, b, c], FakeClock())).provider == "c"


def test_request_id_is_passed_to_provider_switched() -> None:
    a = llm("a", ProviderUnavailable("down"))
    b = llm("b", ok("b"))
    bus = EventBus()
    with bus.subscribe() as sub:
        LLMChain([a, b], FakeClock(), bus).chat(MSGS, timeout_s=5, request_id="req-1")
        event = sub.queue.get_nowait()
    assert isinstance(event, ProviderSwitched) and event.request_id == "req-1"


def test_concurrent_stale_failure_does_not_double_backoff() -> None:
    clock = FakeClock()
    chain_holder: list[LLMChain] = []

    class Racy:
        name = "racy"

        def chat(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolSchema] = (),
            *,
            timeout_s: float,
        ) -> ChatResponse:
            # While this call is in flight, a concurrent request records a failure for us.
            clock.advance(1)
            chain_holder[0]._fail("racy", None, started=clock())
            raise ProviderUnavailable("down")

    chain = LLMChain([Racy(), llm("b", *[ok("b")] * 3)], clock)
    chain_holder.append(chain)
    clock.advance(1)
    chat(chain)
    # Two failures for one outage: the cooldown is still the first step (60 s), not 120 s.
    clock.advance(61)
    assert chain._state["racy"].failures == 1
    assert not chain._cooling_down("racy", clock())


def test_tool_use_rejected_is_preserved_when_chain_fails() -> None:
    chain = LLMChain([llm("a", ToolUseRejected("tool_use_failed"))], FakeClock())
    with pytest.raises(ToolUseRejected):
        chat(chain)


def test_empty_response_does_not_cool_the_provider_down() -> None:
    a = llm("a", EmptyResponse("empty"), ok("a"))
    b = llm("b", ok("b"))
    chain = LLMChain([a, b], FakeClock())
    assert chat(chain).provider == "b"  # request error: next provider, no cooldown for a
    assert chat(chain).provider == "a"  # a is still available for the next request
