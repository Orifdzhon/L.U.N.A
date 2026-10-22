"""LLM provider protocol and the message types it exchanges."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSchema:
    """JSON-schema function definition offered to the model."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("a tool message requires tool_call_id")
            if not isinstance(self.content, str):
                raise ValueError("a tool message requires string content")
        elif self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid for role='tool'")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are only valid for role='assistant'")


@dataclass(frozen=True)
class ChatResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    provider: str
    model: str
    finish_reason: str | None = None  # "stop" / "tool_calls" / "length" / ...
    usage: dict[str, int] | None = None  # token counts when the API reports them


@runtime_checkable
class LLMProvider(Protocol):
    """A chat-completion backend.

    Raises (``luna.core.errors``): ``RateLimited``, ``ProviderUnavailable`` (both trigger
    fallback to the next provider), ``ProviderRequestError`` (our fault; retried on another
    provider at most once) or ``ProviderError`` for anything else.

    Tool-calling contract, enforced by every implementation:

    - ``ToolCall.arguments`` is a parsed ``dict`` (providers parse the API's JSON string);
      invalid JSON raises ``ProviderRequestError``.
    - ``ToolCall.id`` values are non-empty and unique within a response; the provider
      generates ids when the API returns empty or duplicate ones.
    - A ``role="tool"`` message must carry ``tool_call_id`` and string ``content``
      (checked by ``Message``).
    - ``ToolCall.arguments`` / ``ToolSchema.parameters`` are treated as read-only.
    - If any message in the history carries untrusted content (``ToolResult.untrusted``),
      the caller must pass ``tools=()`` (ARCHITECTURE section 4.1).
    """

    name: str

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),  # empty = tools disabled for this call
        *,
        timeout_s: float,
    ) -> ChatResponse: ...
