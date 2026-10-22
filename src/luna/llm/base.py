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


@dataclass(frozen=True)
class ChatResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    provider: str
    model: str


@runtime_checkable
class LLMProvider(Protocol):
    """A chat-completion backend.

    Raises ``RateLimited`` / ``ProviderUnavailable`` / ``ProviderError`` (``luna.core.errors``).
    """

    name: str

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),  # empty = tools disabled for this call
        *,
        timeout_s: float,
    ) -> ChatResponse: ...
