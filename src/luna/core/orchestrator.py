"""Orchestrator: one request end to end (ARCHITECTURE sections 1, 4.1, 5, 8).

``handle`` runs the Router and, if it did not handle the request, the Assistant on a daemon
worker thread while the caller waits for the result, the total timeout or the cancel event.

Rules:

- Every ``Reply`` returned by ``handle`` is also published as ``ReplyReady`` (apologies for
  timeout / cancel / error included), so a UI that only listens to events never misses one.
- Event order: ``StatusChanged(THINKING)`` -> ``ReplyReady`` -> ``StatusChanged(IDLE)``;
  after a failure or timeout the last status is ``StatusChanged(ERROR, detail=...)`` instead.
- A turn that timed out or was cancelled is *abandoned*: its ``abandon`` event is set, so the
  Router / Assistant stop at their next checkpoint without running tools or writing history.
- Turns are serialized: the worker holds a turn lock for the whole turn, so the next request
  waits for an orphaned turn to stop; that wait counts against the new request's timeout.
- A cancel that arrives after the work finished does not hide the real reply (its actions
  really happened).
- ``Exception`` from the turn becomes an apology. A ``BaseException`` (e.g. a test fake's
  ``ScriptExhausted``) is re-raised in the caller, never swallowed.

The Assistant's own deadline (15 s by default) is shorter than the total timeout (20 s).
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
MAX_WAIT_S = 0.05  # upper bound of one wait slice (cancel / injected-clock polling)


class _Router(Protocol):
    def route(self, request: Request, *, cancel: threading.Event | None = None) -> RouteResult: ...


class _Assistant(Protocol):
    def answer(
        self,
        request: Request,
        *,
        force_search: bool = False,
        cancel: threading.Event | None = None,
    ) -> Reply: ...


class Orchestrator:
    def __init__(
        self,
        router: _Router,
        assistant: _Assistant,
        bus: EventBus,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        *,
        timer: Clock = time.monotonic,
    ) -> None:
        self._router = router
        self._assistant = assistant
        self._bus = bus
        self._timeout_s = request_timeout_s
        self._timer = timer
        self._turn_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._abandon: threading.Event | None = None

    def handle(self, request: Request, cancel: threading.Event | None = None) -> Reply:
        rid = request.request_id
        lang = detect_lang(request.text, request.lang)
        deadline = self._timer() + self._timeout_s
        self._bus.publish(StatusChanged(Status.THINKING, rid))

        abandon = threading.Event()
        box: list[Reply] = []
        errors: list[BaseException] = []
        done = threading.Event()
        worker = threading.Thread(
            target=self._work,
            args=(request, abandon, box, errors, done),
            name=f"luna-req-{rid[:8]}",
            daemon=True,
        )
        with self._state_lock:
            self._worker, self._abandon = worker, abandon
        try:
            worker.start()
            failure: str | None = None
            while not done.is_set():
                if cancel is not None and cancel.is_set():
                    failure = "cancelled"
                    break
                remaining = deadline - self._timer()
                if remaining <= 0:
                    failure = "timeout"
                    break
                done.wait(min(remaining, MAX_WAIT_S))
            if done.is_set():
                failure = None  # finished: keep the real reply even if cancel came just now
        finally:
            abandon.set()  # whatever happened, the turn must not do anything more

        if failure is None and errors:
            exc = errors[0]
            if not isinstance(exc, Exception):
                self._bus.publish(StatusChanged(Status.IDLE, rid))
                raise exc
            logger.error("Request failed: %s", type(exc).__name__, exc_info=exc)
            failure = "error"

        if failure is None and not box:
            failure = "cancelled"  # the turn was abandoned via close()
        if failure is None:
            reply = box[0]
            self._bus.publish(ReplyReady(rid, reply))
            self._bus.publish(StatusChanged(Status.IDLE, rid))
            return reply

        kind = {"cancelled": "cancelled", "timeout": "timeout"}.get(failure, "gave_up")
        reply = Reply(fallback_text(kind, lang), lang)
        self._bus.publish(ReplyReady(rid, reply))
        if failure == "cancelled":
            self._bus.publish(StatusChanged(Status.IDLE, rid))
        else:
            self._bus.publish(StatusChanged(Status.ERROR, rid, failure))
        return reply

    def close(self, timeout: float | None = None) -> None:
        """Abandon the current turn (if any) and wait for its worker thread to end."""
        with self._state_lock:
            worker, abandon = self._worker, self._abandon
        if abandon is not None:
            abandon.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    def _work(
        self,
        request: Request,
        abandon: threading.Event,
        box: list[Reply],
        errors: list[BaseException],
        done: threading.Event,
    ) -> None:
        try:
            with self._turn_lock:
                if abandon.is_set():
                    return  # abandoned while waiting for the previous turn
                routed = self._router.route(request, cancel=abandon)
                if routed.handled and routed.reply is not None:
                    box.append(routed.reply)
                else:
                    box.append(
                        self._assistant.answer(
                            request, force_search=routed.force_search, cancel=abandon
                        )
                    )
        except BaseException as exc:  # handed to the caller, which decides
            errors.append(exc)
        finally:
            done.set()
