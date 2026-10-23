"""Ordered LLM fallback with per-provider cooldown (ARCHITECTURE section 8)."""

from __future__ import annotations

import threading
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from luna.core.clock import Clock
from luna.core.errors import (
    ConfigError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
    ToolUseRejected,
)
from luna.core.events import EventBus, ProviderSwitched
from luna.llm.base import ChatResponse, LLMProvider, Message, ToolSchema

BASE_COOLDOWN_S = 60.0  # first cooldown, and the default when ``Retry-After`` is missing
MAX_COOLDOWN_S = 900.0  # 15 minutes: upper bound for both backoff and ``Retry-After``
DEMOTION_COOLDOWN_S = 60.0  # flat (no doubling) cooldown for a provider that keeps rejecting
DEMOTION_THRESHOLD = 2  # request errors in a row, each followed by another provider's success


@dataclass
class _State:
    failures: int = 0  # consecutive RateLimited / ProviderUnavailable
    cooldown_until: float = 0.0
    request_failures: int = 0  # consecutive request errors that another provider then answered
    changed_at: float = float("-inf")  # clock time of the last failure/success recorded


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
    - Demotion: if provider X raises a request error and a later provider then answers the same
      request, the fault is X-specific (e.g. Gemini ``thought_signature`` 400, a revoked key).
      After 2 such cases in a row X gets a flat 60 s cooldown; any success of X resets the
      counter. A request that fails everywhere never demotes anyone.
    - ``timeout_s`` is the overall deadline; each provider gets only the remaining time.
    - A success resets the provider's backoff.
    - Concurrency: ``chat()`` may be called from several threads; state is guarded by a lock that
      is never held during a provider call. A failure that finishes after another thread already
      recorded a failure/success for the same provider (state changed since this call started)
      does not double the backoff again, so one outage counts once. There is no half-open probe:
      several requests may call a provider the moment its cooldown expires. Cooldowns are
      measured from the moment the failure is recorded.
    - When every provider fails nothing is published: ``ProviderSwitched`` needs a target
      provider, and "offline mode" appears with the next successful answer.
    """

    name = "chain"

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        clock: Clock,
        bus: EventBus | None = None,
    ) -> None:
        if not providers:
            raise ConfigError("LLMChain needs at least one provider")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ConfigError(f"provider names must be unique: {names}")
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
        request_id: str | None = None,
        exclude: Collection[str] = (),
    ) -> ChatResponse:
        """``exclude`` names providers to skip for this call only (no state change)."""
        deadline = self._clock() + timeout_s
        causes: list[str] = []
        last_error: ProviderError | None = None
        request_error: ProviderError | None = None
        tool_rejected = False  # a provider rejected a tool call made with tools=()
        rejected: list[str] = []  # providers that raised a request error in this request

        for provider in self._providers:
            if provider.name in exclude:
                causes.append(f"{provider.name}: excluded")
                continue
            now = self._clock()
            if self._cooling_down(provider.name, now):
                causes.append(f"{provider.name}: cooldown")
                continue
            remaining = deadline - now
            if remaining <= 0:
                causes.append("deadline exceeded")
                break
            try:
                started = self._clock()
                response = provider.chat(messages, tools, timeout_s=remaining)
            except RateLimited as exc:
                self._fail(provider.name, exc.retry_after_s, started)
                causes.append(f"{provider.name}: RateLimited")
                last_error = exc
            except ProviderUnavailable as exc:
                self._fail(provider.name, None, started)
                causes.append(f"{provider.name}: ProviderUnavailable")
                last_error = exc
            except ProviderError as exc:
                causes.append(f"{provider.name}: {type(exc).__name__}")
                last_error = exc
                rejected.append(provider.name)
                tool_rejected = tool_rejected or isinstance(exc, ToolUseRejected)
                if request_error is not None:
                    break  # second request-class error: stop, do not burn the chain
                request_error = exc
            else:
                self._succeed(provider.name, causes, rejected, request_id)
                return response

        summary = "all LLM providers failed (" + "; ".join(causes) + ")"
        error: ProviderError
        if request_error is not None:
            error = ToolUseRejected(summary) if tool_rejected else ProviderRequestError(summary)
        else:
            error = ProviderUnavailable(summary)
        if last_error is not None:
            raise error from last_error
        raise error

    def _cooling_down(self, name: str, now: float) -> bool:
        with self._lock:
            return now < self._state[name].cooldown_until

    def _fail(self, name: str, retry_after_s: float | None, started: float) -> None:
        with self._lock:
            state = self._state[name]
            now = self._clock()
            # A failure of a call that started before the last recorded change is part of an
            # outage another thread already counted: extend the cooldown, don't double it.
            if state.changed_at <= started:
                state.failures += 1
            backoff = min(BASE_COOLDOWN_S * 2 ** max(state.failures - 1, 0), MAX_COOLDOWN_S)
            if retry_after_s is None:
                cooldown = backoff
            else:
                cooldown = min(max(retry_after_s, 0.0), MAX_COOLDOWN_S)
            state.cooldown_until = max(state.cooldown_until, now + cooldown)
            state.changed_at = now

    def _succeed(
        self, name: str, causes: list[str], rejected: list[str], request_id: str | None
    ) -> None:
        with self._lock:
            now = self._clock()
            self._state[name] = _State(changed_at=now)
            for other in rejected:
                state = self._state[other]
                state.request_failures += 1
                if state.request_failures >= DEMOTION_THRESHOLD:
                    state.cooldown_until = max(state.cooldown_until, now + DEMOTION_COOLDOWN_S)
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
                        request_id=request_id,
                    )
                )
