"""``LLMProvider`` over any OpenAI-compatible chat-completions endpoint (ADR 0002).

One class serves Gemini (OpenAI-compat endpoint), Groq and local servers (LM Studio, Ollama)
through ``base_url``; per-provider differences are small flags in :class:`Quirks`.

Timeouts are per phase (connect/read), not a total deadline. The SDK's own retries are
disabled (``max_retries=0``): fallback between providers is the job of ``LLMChain``.
The API key lives only inside the SDK client and is never logged, put in exception messages
or shown by ``repr()``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import openai
from pydantic import SecretStr

from luna.core.errors import (
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
    ToolUseRejected,
)
from luna.llm.base import ChatResponse, Message, ToolCall, ToolSchema
from luna.logs.redact import redact_text

log = logging.getLogger(__name__)
# The SDK and httpx log URLs and (at DEBUG) request bodies, i.e. user prompts: keep them quiet.
for _noisy in ("openai", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Local servers (LM Studio, Ollama) ignore the key, but the SDK refuses to start without one.
_NO_KEY_PLACEHOLDER = "not-needed"
_MAX_DETAIL_CHARS = 200
# A server asking for a longer pause must not disable the provider for hours (LLMChain cooldown).
MAX_RETRY_AFTER_S = 300.0
# Cap on the vendor extension kept per tool call (serialized); larger payloads are dropped.
MAX_PROVIDER_EXTRA_BYTES = 8192


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
            # trust_env=False: HTTP(S)_PROXY must not reroute localhost (LM Studio) or the key.
            http_client=http_client or httpx.Client(trust_env=False),
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
            return self._parse(completion)
        except ProviderError:
            raise
        except openai.APIStatusError as exc:
            raise self._map_status_error(exc) from None
        except openai.APITimeoutError:
            raise ProviderUnavailable(f"{self.name}: request timed out") from None
        except openai.APIConnectionError:
            raise ProviderUnavailable(f"{self.name}: cannot connect to the server") from None
        except openai.OpenAIError as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}") from None
        except (ValueError, AttributeError, TypeError, KeyError):
            # e.g. a 200 with a non-JSON body or a structurally broken completion; never echo it.
            raise ProviderUnavailable(f"{self.name}: malformed response") from None

    # -- request -------------------------------------------------------------------------------

    def _build_params(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_wire(m, self.name) for m in messages],
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
        if message is None:
            raise ProviderUnavailable(f"{self.name}: response has no message")
        finish_reason = getattr(choice, "finish_reason", None)
        tool_calls = self._parse_tool_calls(
            getattr(message, "tool_calls", None) or [], finish_reason
        )
        text = _content_text(message.content)
        if not text and not tool_calls:
            raise ProviderUnavailable(
                f"{self.name}: empty response (finish_reason={finish_reason})"
            )
        usage = None
        if getattr(completion, "usage", None) is not None:
            usage = {
                key: value
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(value := getattr(completion.usage, key, None), int)
                and not isinstance(value, bool)
            } or None
        return ChatResponse(
            text=text or None,
            tool_calls=tool_calls,
            provider=self.name,
            model=getattr(completion, "model", None) or self.model,
            finish_reason=finish_reason,
            usage=usage,
        )

    def _parse_tool_calls(
        self, raw_calls: Sequence[Any], finish_reason: str | None
    ) -> tuple[ToolCall, ...]:
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
            calls.append(
                ToolCall(
                    call_id,
                    function.name,
                    self._parse_arguments(function, finish_reason),
                    _provider_extra(raw, self.name),
                )
            )
        return tuple(calls)

    def _parse_arguments(self, function: Any, finish_reason: str | None) -> dict[str, Any]:
        raw = function.arguments
        if isinstance(raw, dict):  # some local servers send an object instead of a string
            return dict(raw)
        if raw is None:
            return {}
        if not isinstance(raw, str):
            raise ProviderRequestError(
                f"{self.name}: arguments of tool call {function.name!r} have an unsupported type"
            )
        if not raw.strip():
            return {}  # some servers send "" for tools without parameters
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            hint = (
                " (output truncated at the token limit; raise max_tokens)"
                if finish_reason == "length"
                else ""
            )
            raise ProviderRequestError(
                f"{self.name}: invalid JSON in arguments of tool call {function.name!r}{hint}"
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
        if status >= 500 or status in (408, 409):  # 409 is usually transient
            return ProviderUnavailable(f"{self.name}: server error (HTTP {status})")
        if status in (401, 403):
            return ProviderRequestError(
                f"{self.name}: authentication failed (HTTP {status}); check the API key and access"
            )
        detail = redact_text(str(getattr(exc, "message", "") or ""))[:_MAX_DETAIL_CHARS]
        suffix = f": {detail}" if detail else ""
        if _error_code(exc) == "tool_use_failed":
            return ToolUseRejected(f"{self.name}: request rejected (HTTP {status}){suffix}")
        return ProviderRequestError(f"{self.name}: request rejected (HTTP {status}){suffix}")


def _error_code(exc: openai.APIStatusError) -> str | None:
    """Error ``code`` from the API error body (flat or nested under ``error``), if any."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        for source in (body, inner if isinstance(inner, dict) else {}):
            code = source.get("code")
            if isinstance(code, str):
                return code
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None


def _content_text(content: Any) -> str:
    """Message content as ``str``: plain text, or the text parts of a list of content parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if kind in (None, "text") and isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    raise TypeError("unsupported content type")


def _provider_extra(raw_call: Any, provider: str) -> dict[str, Any] | None:
    """Vendor extension of a tool call, kept verbatim for the next request.

    Gemini returns ``extra_content: {"google": {"thought_signature": ...}}`` and rejects the
    follow-up request when it is missing. The SDK keeps unknown fields as pydantic extras.
    The result is tagged with the producing provider so no other provider echoes it.
    """
    extra = getattr(raw_call, "extra_content", None)
    if extra is None:
        model_extra = getattr(raw_call, "model_extra", None)
        extra = model_extra.get("extra_content") if isinstance(model_extra, dict) else None
    if not isinstance(extra, dict) or not extra:
        return None
    try:
        size = len(json.dumps(extra))
    except (TypeError, ValueError):
        return None
    if size > MAX_PROVIDER_EXTRA_BYTES:
        log.debug("%s: dropping oversized extra_content (%d bytes)", provider, size)
        return None
    return {"provider": provider, "data": {"extra_content": dict(extra)}}


def _extra_content_for(call: ToolCall, provider: str) -> dict[str, Any] | None:
    """``extra_content`` to echo for ``call``, only if this provider produced it."""
    extra = call.provider_extra
    if not isinstance(extra, Mapping) or extra.get("provider") != provider:
        return None
    data = extra.get("data")
    content = data.get("extra_content") if isinstance(data, dict) else None
    return content if isinstance(content, dict) and content else None


def _message_to_wire(message: Message, provider: str) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = []
        for call in message.tool_calls:
            wire_call: dict[str, Any] = {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            extra = _extra_content_for(call, provider)
            if extra:
                wire_call["extra_content"] = extra
            wire["tool_calls"].append(wire_call)
    return wire


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds (delta-seconds or HTTP date), capped; garbage is ``None``."""
    if not value:
        return None
    value = value.strip()
    try:
        if "_" in value:  # float() would accept "1_0"
            raise ValueError(value)
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
    return min(max(0.0, seconds), MAX_RETRY_AFTER_S)
