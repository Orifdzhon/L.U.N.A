from __future__ import annotations

import threading

import pytest

from fakes import FakeClock, FakeSTT, ScriptExhausted
from luna.core.errors import (
    ConfigError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
)
from luna.core.events import EventBus, ProviderSwitched
from luna.stt.base import STTProvider, Transcript, normalize_lang
from luna.stt.chain import STTChain

PCM = b"\x00\x00" * 16_000  # 1 s at 16 kHz


def ok(text: str = "привет", lang: str = "ru") -> Transcript:
    return Transcript(text, lang, 0.9)


def stt(name: str, *script: Transcript | Exception) -> FakeSTT:
    return FakeSTT(script, name=name)


def run(chain: STTChain, timeout_s: float = 5.0) -> Transcript:
    return chain.transcribe(PCM, timeout_s=timeout_s)


def test_is_an_stt_provider() -> None:
    assert isinstance(STTChain([stt("a")], FakeClock()), STTProvider)


def test_rejects_empty_and_duplicate_providers() -> None:
    with pytest.raises(ConfigError):
        STTChain([], FakeClock())
    with pytest.raises(ConfigError):
        STTChain([stt("a"), stt("a")], FakeClock())


def test_first_provider_answers_and_transcript_is_stamped() -> None:
    a, b = stt("a", ok()), stt("b")
    result = STTChain([a, b], FakeClock()).transcribe(
        PCM, 16_000, lang_hint="ru", prompt="Луна", timeout_s=5
    )
    assert result.text == "привет"
    assert result.lang == "ru"
    assert result.confidence == 0.9
    assert result.provider == "a"
    assert result.duration_s == pytest.approx(1.0)
    assert b.calls == []
    call = a.calls[0]
    assert (call.pcm16, call.sample_rate, call.lang_hint, call.prompt) == (
        PCM,
        16_000,
        "ru",
        "Луна",
    )
    assert call.timeout_s == 5


def test_empty_transcript_is_returned_not_raised_and_not_retried() -> None:
    a, b = stt("a", Transcript("  \n", "ru")), stt("b")
    result = run(STTChain([a, b], FakeClock()))
    assert result.text == ""
    assert result.is_empty
    assert result.lang == "unknown"
    assert b.calls == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ru", "ru"),
        ("en-US", "en"),
        ("Russian", "ru"),
        ("English", "en"),
        ("de", "unknown"),
        ("", "unknown"),
    ],
)
def test_language_is_normalised(raw: str, expected: str) -> None:
    assert normalize_lang(raw) == expected
    assert run(STTChain([stt("a", Transcript("x", raw))], FakeClock())).lang == expected


def test_rate_limited_falls_back_and_skips_until_cooldown_passes() -> None:
    clock = FakeClock()
    a = stt("a", RateLimited(retry_after_s=30), ok())
    b = stt("b", ok(), ok(), ok())
    chain = STTChain([a, b], clock)

    assert run(chain).provider == "b"
    assert len(a.calls) == 1
    clock.advance(29)
    assert run(chain).provider == "b"
    assert len(a.calls) == 1
    clock.advance(1.1)
    assert run(chain).provider == "a"
    assert len(a.calls) == 2


def test_unavailable_uses_default_backoff_that_doubles() -> None:
    clock = FakeClock()
    a = stt("a", ProviderUnavailable("x"), ProviderUnavailable("x"), ok())
    b = stt("b", *[ok()] * 6)
    chain = STTChain([a, b], clock)
    run(chain)
    clock.advance(59)
    run(chain)
    assert len(a.calls) == 1
    clock.advance(2)  # 60 s passed: a fails again -> 120 s
    run(chain)
    assert len(a.calls) == 2
    clock.advance(119)
    run(chain)
    assert len(a.calls) == 2
    clock.advance(2)
    assert run(chain).provider == "a"


def test_all_failing_raises_unavailable_immediately_with_names_and_types_only() -> None:
    clock = FakeClock()
    a = stt("a", RateLimited("secret-a"))
    b = stt("b", ProviderUnavailable("secret-b https://x?key=123"))
    chain = STTChain([a, b], clock)
    with pytest.raises(ProviderUnavailable) as info:
        run(chain)
    message = str(info.value)
    assert "a: RateLimited" in message
    assert "b: ProviderUnavailable" in message
    assert "secret" not in message
    assert "key=123" not in message
    # both on cooldown now: still immediate, providers are not called again
    with pytest.raises(ProviderUnavailable, match="cooldown"):
        run(chain)
    assert len(a.calls) == len(b.calls) == 1


def test_request_error_is_tried_on_exactly_one_more_provider() -> None:
    a = stt("a", ProviderRequestError("bad"))
    b = stt("b", ProviderRequestError("bad"))
    c = stt("c", ok())
    with pytest.raises(ProviderRequestError):
        run(STTChain([a, b, c], FakeClock()))
    assert len(a.calls) == len(b.calls) == 1
    assert c.calls == []


def test_request_error_then_success_on_next_provider_without_cooldown() -> None:
    a = stt("a", ProviderRequestError("bad"), ok())
    b = stt("b", ok())
    chain = STTChain([a, b], FakeClock())
    assert run(chain).provider == "b"
    assert run(chain).provider == "a"  # no cooldown after a single request error


def test_plain_provider_error_counts_as_request_error() -> None:
    a = stt("a", ProviderError("odd"))
    b = stt("b", ProviderError("odd"))
    with pytest.raises(ProviderRequestError):
        run(STTChain([a, b], FakeClock()))


def test_unavailable_then_request_error_still_gets_one_extra_provider() -> None:
    a = stt("a", ProviderUnavailable("down"))
    b = stt("b", ProviderRequestError("bad"))
    c = stt("c", ok())
    assert run(STTChain([a, b, c], FakeClock())).provider == "c"


def test_demotion_after_two_request_errors_answered_elsewhere() -> None:
    clock = FakeClock()
    a = stt("a", ProviderRequestError("x"), ProviderRequestError("x"), ok())
    b = stt("b", ok(), ok(), ok())
    chain = STTChain([a, b], clock)
    run(chain)
    run(chain)
    assert len(a.calls) == 2
    assert run(chain).provider == "b"  # a is demoted
    assert len(a.calls) == 2
    clock.advance(60.1)
    assert run(chain).provider == "a"


def test_overall_deadline_and_remaining_time_per_provider() -> None:
    clock = FakeClock()

    class Slow(FakeSTT):
        def transcribe(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            clock.advance(3)
            return super().transcribe(*args, **kwargs)

    a = Slow([ProviderUnavailable("slow")], name="a")
    b = stt("b", ok())
    c = stt("c", ok())
    chain = STTChain([a, b, c], clock)
    assert run(chain, timeout_s=5).provider == "b"
    assert b.calls[0].timeout_s == pytest.approx(2.0)

    clock2 = FakeClock()

    class Slow2(FakeSTT):
        def transcribe(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            clock2.advance(6)
            return super().transcribe(*args, **kwargs)

    x = Slow2([ProviderUnavailable("slow")], name="x")
    y = stt("y", ok())
    with pytest.raises(ProviderUnavailable, match="deadline exceeded"):
        run(STTChain([x, y], clock2), timeout_s=5)
    assert y.calls == []


def test_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_sleep(*_: object) -> None:
        raise AssertionError("STTChain must never sleep")

    monkeypatch.setattr("time.sleep", no_sleep)
    chain = STTChain([stt("a", RateLimited()), stt("b", ProviderUnavailable("x"))], FakeClock())
    with pytest.raises(ProviderUnavailable):
        run(chain)


def test_non_provider_exceptions_propagate_and_base_exceptions_are_not_caught() -> None:
    with pytest.raises(ValueError):
        run(STTChain([stt("a", ValueError("bug")), stt("b", ok())], FakeClock()))
    with pytest.raises(ScriptExhausted):
        run(STTChain([stt("a"), stt("b", ok())], FakeClock()))


def test_publishes_provider_switched() -> None:
    bus = EventBus()
    sub = bus.subscribe().queue
    clock = FakeClock()
    a = stt("a", RateLimited(retry_after_s=10), ok())
    b = stt("b", ok(), ok())
    chain = STTChain([a, b], clock, bus)

    assert run(chain).provider == "b"
    event = sub.get(timeout=1)
    assert event == ProviderSwitched(
        from_provider=None, to_provider="b", reason="a: RateLimited", request_id=None
    )
    run(chain)  # same provider again: no event
    clock.advance(11)
    chain.transcribe(PCM, timeout_s=5, request_id="r1")
    back = sub.get(timeout=1)
    assert isinstance(back, ProviderSwitched)
    assert (back.from_provider, back.to_provider, back.request_id) == ("b", "a", "r1")
    assert sub.empty()


def test_no_event_when_first_provider_answers_or_all_fail() -> None:
    bus = EventBus()
    sub = bus.subscribe().queue
    assert run(STTChain([stt("a", ok())], FakeClock(), bus)).provider == "a"
    with pytest.raises(ProviderUnavailable):
        run(STTChain([stt("z", ProviderUnavailable("x"))], FakeClock(), bus))
    assert sub.empty()


def test_concurrent_stale_failure_does_not_double_backoff() -> None:
    clock = FakeClock()
    holder: list[STTChain] = []

    class Racy:
        name = "racy"

        def transcribe(
            self,
            pcm16: bytes,
            sample_rate: int = 16_000,
            *,
            lang_hint: str | None = None,
            prompt: str | None = None,
            timeout_s: float,
        ) -> Transcript:
            # While this call is in flight, a concurrent request records a failure for us.
            clock.advance(1)
            holder[0]._fail("racy", None, started=clock())
            raise ProviderUnavailable("down")

    chain = STTChain([Racy(), stt("b", *[ok()] * 3)], clock)
    holder.append(chain)
    clock.advance(1)
    run(chain)
    clock.advance(61)  # one outage: the first cooldown step (60 s), not 120 s
    assert chain._state["racy"].failures == 1
    assert not chain._cooling_down("racy", clock())


def test_parallel_calls_are_safe() -> None:
    chain = STTChain([stt("a", *[ok()] * 40)], FakeClock())
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(run(chain).provider)) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert results == ["a"] * 8
