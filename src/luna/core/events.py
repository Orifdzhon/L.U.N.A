"""Thread-safe event bus: one ``queue.Queue`` per subscriber."""

from __future__ import annotations

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
    request_id: str | None = None
    detail: str | None = None  # e.g. the reason for Status.ERROR


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
    request_id: str | None = None


Event = StatusChanged | TranscriptReady | ReplyReady | ActionExecuted | ProviderSwitched


class _Closed:
    """Type of the ``CLOSED`` sentinel that wakes a blocked consumer."""

    def __repr__(self) -> str:
        return "CLOSED"


CLOSED = _Closed()

QueueItem = Event | _Closed


class Subscription:
    """A subscriber's handle: read from ``queue``, call ``close()`` when done.

    ``close()`` detaches the subscriber and puts ``CLOSED`` into its queue so a
    consumer blocked in ``queue.get()`` wakes up and can exit. It is idempotent.
    Use it as a context manager (or call ``close()`` in ``finally``) so a dead
    consumer never leaves a queue that grows forever.
    """

    def __init__(self, bus: EventBus, q: queue.Queue[QueueItem]) -> None:
        self._bus = bus
        self.queue = q

    def close(self) -> None:
        self._bus._detach(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class EventBus:
    """Fan-out of events to any number of subscribers.

    Each subscriber owns a ``queue.Queue`` and drains it on its own thread, so
    publishers never block on slow consumers. Queues are deliberately unbounded:
    dropping a status event is worse than buffering it, so consumers must close
    their subscription. Events from a single publishing thread arrive in publish
    order, and all subscribers see the same global order.

    The bus remembers the latest ``StatusChanged``; a new subscriber's queue is
    seeded with it so late joiners know the current state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscription] = []
        self._closed = False
        self.last_status: StatusChanged | None = None

    def subscribe(self) -> Subscription:
        sub = Subscription(self, queue.Queue())
        with self._lock:
            if self.last_status is not None:
                sub.queue.put_nowait(self.last_status)
            if self._closed:
                sub.queue.put_nowait(CLOSED)
            else:
                self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Same as ``sub.close()``."""
        sub.close()

    def _detach(self, sub: Subscription) -> None:
        with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                return  # already closed
            sub.queue.put_nowait(CLOSED)

    def publish(self, event: Event) -> None:
        # Enqueue under the lock so every subscriber sees the same global order,
        # even with concurrent publishers. put_nowait on an unbounded queue never
        # blocks or raises; if queues are ever bounded, handle queue.Full per queue.
        with self._lock:
            if self._closed:
                return
            if isinstance(event, StatusChanged):
                self.last_status = event
            for sub in self._subscribers:
                sub.queue.put_nowait(event)

    def close(self) -> None:
        """Close every subscription and ignore later ``publish`` calls."""
        with self._lock:
            self._closed = True
            subs, self._subscribers = self._subscribers, []
            for sub in subs:
                sub.queue.put_nowait(CLOSED)
