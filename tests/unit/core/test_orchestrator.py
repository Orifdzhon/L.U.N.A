import threading

import pytest

from fakes import FakeClock, FakeLLM, FakeTool, ScriptExhausted
from luna.core.assistant import Assistant
from luna.core.events import CLOSED, EventBus, ReplyReady, Status, StatusChanged
from luna.core.models import Reply, Request
from luna.core.orchestrator import Orchestrator
from luna.core.router import Router, RouteResult
from luna.llm.base import ChatResponse, ToolCall
from luna.logs.action_log import ActionLog
from luna.tools.base import ToolResult
from luna.tools.registry import ToolRegistry


class FakeRouter:
    def __init__(self, result: RouteResult) -> None:
        self.result = result
        self.calls = 0

    def route(self, request: Request, *, cancel: threading.Event | None = None) -> RouteResult:
        self.calls += 1
        return self.result


class FakeAssistant:
    def __init__(self, reply: Reply | None = None, *, raises: BaseException | None = None) -> None:
        self.reply = reply or Reply("answer", "en", provider="fake")
        self.raises = raises
        self.calls: list[tuple[Request, bool]] = []
        self.started = threading.Event()
        self.release: threading.Event | None = None  # when set, answer() blocks until released

    def answer(
        self,
        request: Request,
        *,
        force_search: bool = False,
        cancel: threading.Event | None = None,
    ) -> Reply:
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
    events = drain(sub)
    assert events == [
        StatusChanged(Status.THINKING, request.request_id),
        ReplyReady(request.request_id, result[0]),
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

    def boom(request: Request, *, cancel: threading.Event | None = None) -> RouteResult:
        raise ValueError("x")

    router.route = boom  # type: ignore[method-assign]
    assert orch.handle(Request("hi", "text")).text


def test_base_exception_is_not_swallowed() -> None:
    orch, _, _, bus = make(assistant=FakeAssistant(raises=ScriptExhausted()))
    sub = bus.subscribe()
    with pytest.raises(ScriptExhausted):
        orch.handle(Request("hi", "text"))
    assert drain(sub)[-1].status is Status.IDLE


def test_cancel_after_completion_keeps_real_reply() -> None:
    orch, _, assistant, _ = make()
    cancel = threading.Event()
    assistant.started = threading.Event()
    orig = assistant.answer

    def answer_then_cancel(*a, **kw):
        reply = orig(*a, **kw)
        cancel.set()
        return reply

    assistant.answer = answer_then_cancel  # type: ignore[method-assign]
    assert orch.handle(Request("hi", "text"), cancel).text == "answer"


def test_close_without_turns_is_noop() -> None:
    make()[0].close()


# --- real Router/Assistant: an abandoned turn must have no side effects ---------------------


class BlockingLLM:
    """Wraps FakeLLM; the first call announces itself and blocks until released."""

    def __init__(self, script) -> None:
        self.inner = FakeLLM(script)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def chat(self, messages, tools=(), *, timeout_s, request_id=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            block, self._first = self._first, False
        try:
            if block:
                self.entered.set()
                assert self.release.wait(10)
            return self.inner.chat(messages, tools, timeout_s=timeout_s, request_id=request_id)
        finally:
            with self._lock:
                self.active -= 1


class Real:
    def __init__(self, tmp_path, script, timeout=20.0) -> None:
        self.llm = BlockingLLM(script)
        registry = ToolRegistry(ActionLog(tmp_path / "actions.jsonl"))
        self.tool = FakeTool(
            "open_app",
            ToolResult("open_app", True, "Opened"),
            parameters={"type": "object", "additionalProperties": True},
        )
        registry.register(self.tool)
        self.assistant = Assistant(self.llm, registry, now_context=lambda: "")
        self.clock = FakeClock()
        self.bus = EventBus()
        self.orch = Orchestrator(
            Router(registry), self.assistant, self.bus, timeout, timer=self.clock
        )

    def start(self, text="hello", cancel=None):
        return run_in_thread(self.orch, Request(text, "text"), cancel)


def tool_call() -> ChatResponse:
    return ChatResponse(None, (ToolCall("c1", "open_app", {"name": "x"}),), "fake", "m")


def say(text: str) -> ChatResponse:
    return ChatResponse(text, (), "fake", "m", "stop")


@pytest.mark.parametrize("how", ["timeout", "cancel"])
def test_abandoned_turn_runs_no_tool_and_writes_no_history(tmp_path, how: str) -> None:
    w = Real(tmp_path, [tool_call(), say("done")])
    cancel = threading.Event()
    t, _ = w.start(cancel=cancel)
    assert w.llm.entered.wait(10)
    if how == "timeout":
        w.clock.advance(100)
    else:
        cancel.set()
    t.join(10)
    assert not t.is_alive()
    w.llm.release.set()  # the LLM now returns a tool call to the abandoned turn
    w.orch.close(10)
    assert w.tool.calls == []
    assert len(w.assistant._history) == 0
    assert len(w.llm.inner.calls) == 1  # no further LLM round either


def test_next_request_waits_for_orphan(tmp_path) -> None:
    w = Real(tmp_path, [say("orphan"), say("second")])
    t1, _ = w.start("first")
    assert w.llm.entered.wait(10)
    w.clock.advance(100)
    t1.join(10)
    t2, result2 = w.start("second")  # its worker queues behind the orphan's turn lock
    w.llm.release.set()
    t2.join(10)
    w.orch.close(10)
    assert not t2.is_alive()
    assert w.llm.max_active == 1
    assert len(w.llm.inner.calls) == 2
    assert result2[0].text == "second"
    assert [text for text, _ in w.assistant._history] == ["second"]
