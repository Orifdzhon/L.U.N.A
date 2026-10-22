"""Request / reply data types passed between the UI adapters and the core."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from luna.tools.base import ToolResult


def new_id() -> str:
    """Return a new unique request id."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Request:
    text: str
    source: Literal["text", "voice"]
    lang: str | None = None  # "ru" / "en" hint from STT
    request_id: str = field(default_factory=new_id)


@dataclass
class Reply:
    text: str
    lang: str  # language to speak in
    actions: list[ToolResult] = field(default_factory=list)
    provider: str | None = None  # which LLM answered, or "router"
