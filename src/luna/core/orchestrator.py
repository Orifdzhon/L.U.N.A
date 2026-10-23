"""Orchestrator: one request end to end (ARCHITECTURE sections 1, 4.1, 5, 8).

``handle`` runs the Router and, if it did not handle the request, the Assistant on a daemon
worker thread while the caller waits for the result, the total timeout or the cancel event.
A timed-out or cancelled turn is abandoned: its result is discarded. ``handle`` never raises
``Exception`` to the caller; a ``BaseException`` from the worker (e.g. a test fake's
``ScriptExhausted``) is re-raised in the caller, never swallowed.

Events per request: ``StatusChanged(THINKING)`` -> ``ReplyReady`` -> ``StatusChanged(IDLE)``.
On failure or timeout the apology is still published and the final status is
``StatusChanged(ERROR, detail=...)``. On cancel no reply is published, only ``IDLE``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

from luna.core.clock import Clock
from luna.core.events import EventBus, ReplyReady, Status, StatusChanged
from luna.core.models import Reply, Request
from luna.core.prompts import fallback_text
from luna.core.router import RouteResult, detect_lang

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT_S = 20.0
POLL_INTERVAL_S = 0.01


class _Router(Protocol):
    def route(self, request: Request) -> RouteResult: ...


class _Assistant(Protocol):
    def answer(self, request: Request, *, force_search: bool = False) -> Reply: ...


class Orchestrator:
    def __init__(
        self,
        router: _Router,
        assistant: _Assistant,
        bus: EventBus,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        *,
        timer: Clock = time.monotonic,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._router = router
        self._assistant = assistant
        self._bus = bus
        self._timeout_s = request_timeout_s
        self._timer = timer
        self._poll_s = poll_interval_s

    def handle(self, request: Request, cancel: threading.Event | None = None) -> Reply:
        rid = request.request_id
        lang = detect_lang(request.text, request.lang)
        self._bus.publish(StatusChanged(Status.THINKING, rid))

        box: list[Reply] = []
        errors: list[BaseException] = []
        done = threading.Event()
        threading.Thread(
            target=self._work,
            args=(request, box, errors, done),
            name=f"luna-req-{rid[:8]}",
            daemon=True,
        ).start()

        deadline = self._timer() + self._timeout_s
        failure: str | None = None
        while not done.wait(self._poll_s):
            if cancel is not None and cancel.is_set():
                failure = "cancelled"
                break
            if self._timer() >= deadline:
                failure = "timeout"
                break
        if failure is None and cancel is not None and cancel.is_set():
            failure = "cancelled"  # cancelled just as the work finished: discard the result

        if failure is None and errors:
            exc = errors[0]
            if not isinstance(exc, Exception):
                self._bus.publish(StatusChanged(Status.IDLE, rid))
                raise exc
            logger.error("Request failed: %s", type(exc).__name__, exc_info=exc)
            failure = "error"

        if failure == "cancelled":
            reply = Reply(fallback_text("cancelled", lang), lang)
            self._bus.publish(StatusChanged(Status.IDLE, rid))
            return reply
        if failure is not None:
            kind = "timeout" if failure == "timeout" else "gave_up"
            reply = Reply(fallback_text(kind, lang), lang)
            self._bus.publish(ReplyReady(rid, reply))
            self._bus.publish(StatusChanged(Status.ERROR, rid, failure))
            return reply

        reply = box[0]
        self._bus.publish(ReplyReady(rid, reply))
        self._bus.publish(StatusChanged(Status.IDLE, rid))
        return reply

    def _work(
        self,
        request: Request,
        box: list[Reply],
        errors: list[BaseException],
        done: threading.Event,
    ) -> None:
        try:
            routed = self._router.route(request)
            if routed.handled and routed.reply is not None:
                box.append(routed.reply)
            else:
                box.append(self._assistant.answer(request, force_search=routed.force_search))
        except BaseException as exc:  # handed to the caller, which decides
            errors.append(exc)
        finally:
            done.set()
