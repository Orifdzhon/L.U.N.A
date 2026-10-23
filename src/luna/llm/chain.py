"""Ordered LLM fallback with per-provider cooldown (ARCHITECTURE section 8)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from luna.core.clock import Clock
from luna.core.errors import (
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
)
from luna.core.events import EventBus, ProviderSwitched
from luna.llm.base import ChatResponse, LLMProvider, Message, ToolSchema

BASE_COOLDOWN_S = 60.0  # first cooldown, and the default when ``Retry-After`` is missing
MAX_COOLDOWN_S = 900.0  # 15 minutes: upper bound for both backoff and ``Retry-After``


@dataclass
class _State:
    failures: int = 0  # consecutive RateLimited / ProviderUnavailable
    cooldown_until: float = 0.0


class LLMChain:
    """Tries providers in order and skips the ones on cooldown.

    - ``RateLimited`` / ``ProviderUnavailable``: the provider is put on cooldown
      (``Retry-After`` if given, otherwise 60 s doubling per consecutive failure, capped at
      900 s) and the next provider is tried within the same request.
    - ``ProviderRequestError`` (and other plain ``ProviderError``): our fault, no cooldown. The
      request is tried on one more provider; a second such error is raised as is, so a bad
      request does not burn the whole chain.
    - A request never waits: providers on cooldown are skipped, and when nothing is left
      ``ProviderUnavailable`` (or ``ProviderRequestError``, if one happened) is raised at once.
      The summary lists provider names and error types only, never error messages.
    - ``timeout_s`` is the overall deadline; each provider gets only the remaining time.
    - A success resets the provider's backoff.
    """

    name = "chain"

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        clock: Clock,
        bus: EventBus | None = None,
    ) -> None:
        if not providers:
            raise ValueError("LLMChain needs at least one provider")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError(f"provider names must be unique: {names}")
        self._providers = list(providers)
        self._clock = clock
        self._bus = bus
        self._lock = threading.Lock()
        self._state = {p.name: _State() for p in self._providers}
        self._last_answered: str | None = None

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
    ) -> ChatResponse:
        deadline = self._clock() + timeout_s
        causes: list[str] = []
        last_error: ProviderError | None = None
        request_error: ProviderError | None = None

        for provider in self._providers:
            now = self._clock()
            if self._cooling_down(provider.name, now):
                causes.append(f"{provider.name}: cooldown")
                continue
            remaining = deadline - now
            if remaining <= 0:
                causes.append("deadline exceeded")
                break
            try:
                response = provider.chat(messages, tools, timeout_s=remaining)
            except RateLimited as exc:
                self._fail(provider.name, exc.retry_after_s)
                causes.append(f"{provider.name}: RateLimited")
                last_error = exc
            except ProviderUnavailable as exc:
                self._fail(provider.name, None)
                causes.append(f"{provider.name}: ProviderUnavailable")
                last_error = exc
            except ProviderError as exc:
                causes.append(f"{provider.name}: {type(exc).__name__}")
                if request_error is not None:
                    raise  # second request-class error: stop, do not burn the chain
                request_error = last_error = exc
            else:
                self._succeed(provider.name, causes)
                return response

        summary = "all LLM providers failed (" + "; ".join(causes) + ")"
        error: ProviderError
        if request_error is not None:
            error = ProviderRequestError(summary)
        else:
            error = ProviderUnavailable(summary)
        if last_error is not None:
            raise error from last_error
        raise error

    def _cooling_down(self, name: str, now: float) -> bool:
        with self._lock:
            return now < self._state[name].cooldown_until

    def _fail(self, name: str, retry_after_s: float | None) -> None:
        with self._lock:
            state = self._state[name]
            state.failures += 1
            backoff = min(BASE_COOLDOWN_S * 2 ** (state.failures - 1), MAX_COOLDOWN_S)
            if retry_after_s is None:
                cooldown = backoff
            else:
                cooldown = min(max(retry_after_s, 0.0), MAX_COOLDOWN_S)
            state.cooldown_until = self._clock() + cooldown

    def _succeed(self, name: str, causes: list[str]) -> None:
        with self._lock:
            self._state[name] = _State()
            previous = self._last_answered
            self._last_answered = name
        if previous == name or (previous is None and name == self._providers[0].name):
            return
        if self._bus is not None:
            self._bus.publish(
                ProviderSwitched(
                    from_provider=previous,
                    to_provider=name,
                    reason="; ".join(causes) or "recovered",
                )
            )
