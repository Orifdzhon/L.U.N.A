"""Thread-safe event bus: one ``queue.Queue`` per subscriber."""

from __future__ import annotations

import contextlib
import enum
import queue
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from luna.core.models import Reply
    from luna.tools.base import ToolResult


class Status(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass(frozen=True)
class StatusChanged:
    status: Status


@dataclass(frozen=True)
class TranscriptReady:
    request_id: str
    text: str
    lang: str | None = None


@dataclass(frozen=True)
class ReplyReady:
    request_id: str
    reply: Reply


@dataclass(frozen=True)
class ActionExecuted:
    request_id: str
    result: ToolResult


@dataclass(frozen=True)
class ProviderSwitched:
    from_provider: str | None
    to_provider: str
    reason: str = ""


Event = StatusChanged | TranscriptReady | ReplyReady | ActionExecuted | ProviderSwitched


class EventBus:
    """Fan-out of events to any number of subscribers.

    Each subscriber owns an unbounded ``queue.Queue`` and drains it on its own
    thread, so publishers never block on slow consumers. Events from a single
    publishing thread arrive in publish order.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event]] = []

    def subscribe(self) -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event]) -> None:
        """Stop delivering to ``q``. Unknown queues are ignored."""
        with self._lock, contextlib.suppress(ValueError):
            self._subscribers.remove(q)

    def publish(self, event: Event) -> None:
        # Hold the lock while enqueueing so every subscriber sees the same
        # global order of events, even with concurrent publishers.
        with self._lock:
            for q in self._subscribers:
                q.put_nowait(event)
