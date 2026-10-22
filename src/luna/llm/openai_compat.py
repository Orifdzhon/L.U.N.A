"""``LLMProvider`` over any OpenAI-compatible chat-completions endpoint (ADR 0002).

One class serves Gemini (OpenAI-compat endpoint), Groq and local servers (LM Studio, Ollama)
through ``base_url``; per-provider differences are small flags in :class:`Quirks`.

The SDK's own retries are disabled (``max_retries=0``): fallback between providers is the job
of ``LLMChain``. The API key lives only inside the SDK client and is never logged, put in
exception messages or shown by ``repr()``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import openai
from pydantic import SecretStr

from luna.core.errors import ProviderError, ProviderRequestError, ProviderUnavailable, RateLimited
from luna.llm.base import ChatResponse, Message, ToolCall, ToolSchema
from luna.logs.redact import redact_text

log = logging.getLogger(__name__)

# Local servers (LM Studio, Ollama) ignore the key, but the SDK refuses to start without one.
_NO_KEY_PLACEHOLDER = "not-needed"
_MAX_DETAIL_CHARS = 200


@dataclass(frozen=True)
class Quirks:
    """Optional per-provider request parameters; ``None`` means "do not send"."""

    supports_tool_choice: bool | None = None  # only ``True`` ever sends ``tool_choice="auto"``
    reasoning_effort: str | None = None
    max_tokens: int | None = None


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: SecretStr | str | None = None,
        quirks: Quirks | None = None,
        *,
        connect_timeout_s: float = 5.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._base_url = base_url
        self._quirks = quirks or Quirks()
        self._connect_timeout_s = connect_timeout_s
        if isinstance(api_key, SecretStr):
            api_key = api_key.get_secret_value()
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=(api_key or "").strip() or _NO_KEY_PLACEHOLDER,
            max_retries=0,
            http_client=http_client,
        )

    def __repr__(self) -> str:
        return f"OpenAICompatProvider(name={self.name!r}, model={self.model!r})"

    def close(self) -> None:
        self._client.close()

    # -- LLMProvider ---------------------------------------------------------------------------

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
    ) -> ChatResponse:
        params = self._build_params(messages, tools)
        timeout = httpx.Timeout(timeout_s, connect=min(self._connect_timeout_s, timeout_s))
        log.debug(
            "%s: chat model=%s messages=%d tools=%d",
            self.name,
            self.model,
            len(messages),
            len(tools),
        )
        try:
            completion = self._client.chat.completions.create(timeout=timeout, **params)
        except openai.APIStatusError as exc:
            raise self._map_status_error(exc) from None
        except openai.APITimeoutError:
            raise ProviderUnavailable(f"{self.name}: request timed out") from None
        except openai.APIConnectionError:
            raise ProviderUnavailable(f"{self.name}: cannot connect to the server") from None
        except openai.OpenAIError as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}") from None
        return self._parse(completion)

    # -- request -------------------------------------------------------------------------------

    def _build_params(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_wire(m) for m in messages],
        }
        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            if self._quirks.supports_tool_choice is True:
                params["tool_choice"] = "auto"
        if self._quirks.reasoning_effort:
            params["reasoning_effort"] = self._quirks.reasoning_effort
        if self._quirks.max_tokens:
            params["max_tokens"] = self._quirks.max_tokens
        return params

    # -- response ------------------------------------------------------------------------------

    def _parse(self, completion: Any) -> ChatResponse:
        choices = getattr(completion, "choices", None)
        if not choices:
            raise ProviderUnavailable(f"{self.name}: response has no choices")
        choice = choices[0]
        message = choice.message
        tool_calls = self._parse_tool_calls(getattr(message, "tool_calls", None) or [])
        usage = None
        if completion.usage is not None:
            usage = {
                key: value
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(value := getattr(completion.usage, key, None), int)
            }
        return ChatResponse(
            text=message.content or None,
            tool_calls=tool_calls,
            provider=self.name,
            model=getattr(completion, "model", None) or self.model,
            finish_reason=choice.finish_reason,
            usage=usage,
        )

    def _parse_tool_calls(self, raw_calls: Sequence[Any]) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        seen: set[str] = set()
        for raw in raw_calls:
            function = getattr(raw, "function", None)
            if function is None or not function.name:
                raise ProviderRequestError(f"{self.name}: tool call without a function name")
            call_id = (raw.id or "").strip()
            while not call_id or call_id in seen:
                call_id = f"call_{uuid.uuid4().hex[:12]}"
            seen.add(call_id)
            calls.append(ToolCall(call_id, function.name, self._parse_arguments(function)))
        return tuple(calls)

    def _parse_arguments(self, function: Any) -> dict[str, Any]:
        raw = function.arguments
        if raw is None or not raw.strip():
            return {}  # some servers send "" for tools without parameters
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderRequestError(
                f"{self.name}: invalid JSON in arguments of tool call {function.name!r}"
            ) from None
        if not isinstance(arguments, dict):
            raise ProviderRequestError(
                f"{self.name}: arguments of tool call {function.name!r} are not a JSON object"
            )
        return arguments

    # -- errors --------------------------------------------------------------------------------

    def _map_status_error(self, exc: openai.APIStatusError) -> ProviderError:
        status = exc.status_code
        if status == 429:
            retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
            return RateLimited(f"{self.name}: rate limited (HTTP 429)", retry_after_s=retry_after)
        if status >= 500 or status == 408:
            return ProviderUnavailable(f"{self.name}: server error (HTTP {status})")
        if status in (401, 403):
            return ProviderRequestError(
                f"{self.name}: authentication failed (HTTP {status}); check the API key and access"
            )
        detail = redact_text(str(getattr(exc, "message", "") or ""))[:_MAX_DETAIL_CHARS]
        suffix = f": {detail}" if detail else ""
        return ProviderRequestError(f"{self.name}: request rejected (HTTP {status}){suffix}")


def _message_to_wire(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return wire


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds: delta-seconds or an HTTP date; anything else is ``None``."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(0.0, seconds)
