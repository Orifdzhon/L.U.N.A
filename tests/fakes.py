"""Tiny in-memory fakes for the protocols in ``luna.*.base`` (test infrastructure only)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from luna.llm.base import ChatResponse, Message, ToolSchema
from luna.platform.base import AppEntry
from luna.search.base import SearchResult
from luna.tools.base import SafetyLevel, ToolContext, ToolResult


class FakeLLM:
    """Replays a scripted list of responses; an ``Exception`` item is raised instead."""

    def __init__(self, script: Sequence[ChatResponse | Exception], name: str = "fake") -> None:
        self.name = name
        self._script = list(script)
        self.calls: list[tuple[list[Message], list[ToolSchema]]] = []

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
    ) -> ChatResponse:
        self.calls.append((list(messages), list(tools)))
        if not self._script:
            raise AssertionError("FakeLLM script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSearch:
    def __init__(
        self,
        results: Sequence[SearchResult] = (),
        error: Exception | None = None,
        name: str = "fake",
    ) -> None:
        self.name = name
        self.results = list(results)
        self.error = error
        self.queries: list[str] = []

    def search(
        self, query: str, *, lang: str, max_results: int = 5, timeout_s: float = 5.0
    ) -> list[SearchResult]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.results[:max_results]


class FakeAppCatalog:
    def __init__(self, apps: Sequence[AppEntry] = ()) -> None:
        self.apps = list(apps)
        self.refresh_count = 0

    def list_apps(self) -> list[AppEntry]:
        return list(self.apps)

    def refresh(self) -> None:
        self.refresh_count += 1


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[AppEntry] = []

    def launch(self, app: AppEntry) -> None:
        self.launched.append(app)


class FakeBrowser:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def open_url(self, url: str) -> None:
        self.urls.append(url)


class FakeConfirmer:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def confirm(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.answer


class FakeClock:
    """Manual clock: time moves only through ``advance()``."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    monotonic = now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self._now += seconds


class FakeTool:
    def __init__(
        self,
        name: str = "fake_tool",
        result: ToolResult | None = None,
        safety: SafetyLevel = SafetyLevel.SAFE,
        description: str = "A fake tool.",
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters if parameters is not None else {"type": "object"}
        self.safety = safety
        self.result = result or ToolResult(tool=name, ok=True, summary="ok")
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append((args, ctx))
        return self.result
