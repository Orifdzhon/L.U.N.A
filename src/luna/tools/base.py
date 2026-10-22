"""Tool protocol, safety levels and the context passed to tools."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


class SafetyLevel(enum.Enum):
    SAFE = "safe"  # execute immediately
    SENSITIVE = "sensitive"  # needs confirmation (no such tools in MVP)
    DANGEROUS = "dangerous"  # explicit on-screen confirmation; registry refuses in MVP


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    summary: str  # short text for the LLM / user
    data: dict[str, Any] = field(default_factory=dict)
    untrusted: bool = False  # True for web content -> disables tools on the next LLM call


@runtime_checkable
class Confirmer(Protocol):
    """Provided by the UI: the CLI asks y/n, Qt shows a dialog."""

    def confirm(self, prompt: str) -> bool: ...


@dataclass(frozen=True)
class ToolContext:
    request_id: str
    source: Literal["text", "voice"]
    confirmer: Confirmer
    user_text: str
    provider: str | None = None  # LLM that issued the call, or "router"


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema
    safety: SafetyLevel

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...
