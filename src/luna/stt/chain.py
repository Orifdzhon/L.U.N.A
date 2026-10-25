"""Ordered STT fallback with per-provider cooldown (ARCHITECTURE section 8).

Mirrors ``luna.llm.chain.LLMChain`` (same constants and semantics) without the LLM-only parts
(tools, ``exclude``); the small state machine is duplicated on purpose so LLMChain stays untouched.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from luna.core.clock import Clock
from luna.core.errors import (
    ConfigError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
)
from luna.core.events import EventBus, ProviderSwitched
from luna.llm.chain import (
    BASE_COOLDOWN_S,
    DEMOTION_COOLDOWN_S,
    DEMOTION_THRESHOLD,
    MAX_COOLDOWN_S,
)
from luna.stt.base import UNKNOWN_LANG, STTProvider, Transcript, normalize_lang

_SAMPLE_WIDTH = 2  # int16


@dataclass
class _State:
    failures: int = 0
    cooldown_until: float = 0.0
    request_failures: int = 0
    changed_at: float = float("-inf")


class STTChain:
    """Tries STT providers in order and skips the ones on cooldown.

    - ``RateLimited`` / ``ProviderUnavailable``: cooldown (``Retry-After`` or 60 s doubling up
      to 900 s) and the next provider within the same call.
    - ``ProviderRequestError`` (any other ``ProviderError``): no cooldown; tried on at most one
      more provider, a second one ends the call with ``ProviderRequestError``.
    - Never sleeps: cooling providers are skipped; when none is left ``ProviderUnavailable`` is
      raised at once. Messages list provider names and error types only.
    - ``timeout_s`` is the overall deadline; each provider gets the remaining time.
    - Demotion and thread-safety work as in ``LLMChain`` (lock never held during a provider call).
    - The returned transcript is stamped with the provider name and the audio duration; the
      language is normalised to ru/en/unknown; an empty transcript is returned, not raised.
    - Only ``ProviderError`` subclasses escape; other exceptions are bugs and propagate untouched.
    """

    name = "chain"

    def __init__(
        self,
        providers: Sequence[STTProvider],
        clock: Clock,
        bus: EventBus | None = None,
    ) -> None:
        if not providers:
            raise ConfigError("STTChain needs at least one provider")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ConfigError(f"provider names must be unique: {names}")
        self._providers = list(providers)
        self._clock = clock
        self._bus = bus
        self._lock = threading.Lock()
        self._state = {p.name: _State() for p in self._providers}
        self._last_answered: str | None = None

    def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = 16_000,
        *,
        lang_hint: str | None = None,
        prompt: str | None = None,
        timeout_s: float,
        request_id: str | None = None,
    ) -> Transcript:
        deadline = self._clock() + timeout_s
        causes: list[str] = []
        last_error: ProviderError | None = None
        request_error: ProviderError | None = None
        rejected: list[str] = []

        for provider in self._providers:
            now = self._clock()
            if self._cooling_down(provider.name, now):
                causes.append(f"{provider.name}: cooldown")
                continue
            remaining = deadline - now
            if remaining <= 0:
                causes.append("deadline exceeded")
                break
            started = now
            try:
                result = provider.transcribe(
                    pcm16,
                    sample_rate,
                    lang_hint=lang_hint,
                    prompt=prompt,
                    timeout_s=remaining,
                )
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
                if request_error is not None:
                    break
                request_error = exc
            else:
                self._succeed(provider.name, causes, rejected, request_id)
                text = result.text.strip()
                return dataclasses.replace(
                    result,
                    text=text,
                    lang=normalize_lang(result.lang) if text else UNKNOWN_LANG,
                    provider=provider.name,
                    duration_s=(
                        len(pcm16) / _SAMPLE_WIDTH / sample_rate if sample_rate > 0 else None
                    ),
                )

        summary = "all STT providers failed (" + "; ".join(causes) + ")"
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

    def _fail(self, name: str, retry_after_s: float | None, started: float) -> None:
        with self._lock:
            state = self._state[name]
            now = self._clock()
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
