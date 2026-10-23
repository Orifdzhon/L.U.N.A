import threading

import pytest

from fakes import FakeClock, ScriptExhausted
from luna.core.events import CLOSED, EventBus, ReplyReady, Status, StatusChanged
from luna.core.models import Reply, Request
from luna.core.orchestrator import Orchestrator
from luna.core.router import RouteResult


class FakeRouter:
    def __init__(self, result: RouteResult) -> None:
        self.result = result
        self.calls = 0

    def route(self, request: Request) -> RouteResult:
        self.calls += 1
        return self.result


class FakeAssistant:
    def __init__(self, reply: Reply | None = None, *, raises: BaseException | None = None) -> None:
        self.reply = reply or Reply("answer", "en", provider="fake")
        self.raises = raises
        self.calls: list[tuple[Request, bool]] = []
        self.started = threading.Event()
        self.release: threading.Event | None = None  # when set, answer() blocks until released

    def answer(self, request: Request, *, force_search: bool = False) -> Reply:
        self.calls.append((request, force_search))
        self.started.set()
        if self.release is not None:
            assert self.release.wait(10)
        if self.raises is not None:
            raise self.raises
        return self.reply


def drain(sub) -> list:
    items = []
    sub.close()
    while (item := sub.queue.get_nowait()) is not CLOSED:
        items.append(item)
    return items


def make(router_result=None, assistant=None, timeout=20.0, **kw):
    bus = EventBus()
    router = FakeRouter(router_result or RouteResult(False))
    assistant = assistant or FakeAssistant()
    return Orchestrator(router, assistant, bus, timeout, **kw), router, assistant, bus


def run_in_thread(
    orch: Orchestrator, request: Request, cancel=None
) -> tuple[threading.Thread, list]:
    result: list[Reply] = []
    t = threading.Thread(target=lambda: result.append(orch.handle(request, cancel)))
    t.start()
    return t, result


def test_router_handled_skips_assistant() -> None:
    reply = Reply("opened", "en", provider="router")
    orch, router, assistant, _ = make(RouteResult(True, reply))
    assert orch.handle(Request("open chrome", "text")) is reply
    assert router.calls == 1
    assert assistant.calls == []


@pytest.mark.parametrize("force", [False, True])
def test_unhandled_reaches_assistant_with_force_search(force: bool) -> None:
    orch, _, assistant, _ = make(RouteResult(False, force_search=force))
    request = Request("weather", "text")
    assert orch.handle(request).text == "answer"
    assert assistant.calls == [(request, force)]


def test_event_order() -> None:
    orch, _, _, bus = make()
    sub = bus.subscribe()
    request = Request("hi", "text")
    reply = orch.handle(request)
    assert drain(sub) == [
        StatusChanged(Status.THINKING, request.request_id),
        ReplyReady(request.request_id, reply),
        StatusChanged(Status.IDLE, request.request_id),
    ]


def test_timeout_returns_apology_and_discards_result() -> None:
    clock = FakeClock()
    assistant = FakeAssistant()
    assistant.release = threading.Event()
    orch, _, _, bus = make(assistant=assistant, timeout=0.2, timer=clock)
    sub = bus.subscribe()
    request = Request("hello", "text")
    t, result = run_in_thread(orch, request)
    assert assistant.started.wait(10)
    clock.advance(0.3)
    t.join(10)
    assistant.release.set()
    assert not t.is_alive()
    assert result[0].text != "answer"
    events = drain(sub)
    assert events[0] == StatusChanged(Status.THINKING, request.request_id)
    assert events[1] == ReplyReady(request.request_id, result[0])
    assert events[2] == StatusChanged(Status.ERROR, request.request_id, "timeout")


def test_cancel_aborts_early() -> None:
    assistant = FakeAssistant()
    assistant.release = threading.Event()
    orch, _, _, bus = make(assistant=assistant)
    sub = bus.subscribe()
    cancel = threading.Event()
    request = Request("привет", "text")
    t, result = run_in_thread(orch, request, cancel)
    assert assistant.started.wait(10)
    cancel.set()
    t.join(10)
    assistant.release.set()
    assert not t.is_alive()
    assert result[0].lang == "ru"
    assert drain(sub) == [
        StatusChanged(Status.THINKING, request.request_id),
        StatusChanged(Status.IDLE, request.request_id),
    ]


def test_unexpected_exception_becomes_apology() -> None:
    orch, _, _, bus = make(assistant=FakeAssistant(raises=RuntimeError("boom")))
    sub = bus.subscribe()
    request = Request("hi", "text")
    reply = orch.handle(request)
    assert reply.text
    events = drain(sub)
    assert events[-1] == StatusChanged(Status.ERROR, request.request_id, "error")
    assert any(isinstance(e, ReplyReady) for e in events)


def test_router_exception_becomes_apology() -> None:
    orch, router, _, _ = make()

    def boom(request: Request) -> RouteResult:
        raise ValueError("x")

    router.route = boom  # type: ignore[method-assign]
    assert orch.handle(Request("hi", "text")).text


def test_base_exception_is_not_swallowed() -> None:
    orch, _, _, bus = make(assistant=FakeAssistant(raises=ScriptExhausted()))
    sub = bus.subscribe()
    with pytest.raises(ScriptExhausted):
        orch.handle(Request("hi", "text"))
    assert drain(sub)[-1].status is Status.IDLE


def test_already_cancelled_event_discards_result() -> None:
    orch, _, _, _ = make()
    cancel = threading.Event()
    cancel.set()
    assert orch.handle(Request("hi", "text"), cancel).text != "answer"
