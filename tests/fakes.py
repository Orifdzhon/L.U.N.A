"""Tiny in-memory fakes for the protocols in ``luna.*.base`` (test infrastructure only).

Import convention: ``pytest`` puts ``tests/`` on ``sys.path`` (see ``pythonpath`` in
``pyproject.toml``), so tests do ``from fakes import FakeLLM``.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, NamedTuple

from luna.core.errors import ToolError
from luna.llm.base import ChatResponse, Message, ToolSchema
from luna.platform.base import AppEntry
from luna.search.base import SearchResult
from luna.tools.base import SafetyLevel, ToolContext, ToolResult


class ScriptExhausted(BaseException):
    """FakeLLM was called more often than scripted.

    Derives from ``BaseException`` on purpose: a broad ``except Exception`` in production code
    (LLMChain, assistant loop) must not swallow it and fall through to another provider.
    """


class LLMCall(NamedTuple):
    messages: list[Message]
    tools: list[ToolSchema]
    timeout_s: float


class FakeLLM:
    """Replays a scripted list of responses; an ``Exception`` item is raised instead."""

    def __init__(self, script: Sequence[ChatResponse | Exception], name: str = "fake") -> None:
        self.name = name
        self._script = list(script)
        self.calls: list[LLMCall] = []

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
    ) -> ChatResponse:
        self.calls.append(
            LLMCall(copy.deepcopy(list(messages)), copy.deepcopy(list(tools)), timeout_s)
        )
        if not self._script:
            raise ScriptExhausted("FakeLLM script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SearchCall(NamedTuple):
    query: str
    lang: str
    max_results: int
    timeout_s: float


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
        self.calls: list[SearchCall] = []

    @property
    def queries(self) -> list[str]:
        return [c.query for c in self.calls]

    def search(
        self, query: str, *, lang: str, max_results: int = 5, timeout_s: float = 5.0
    ) -> list[SearchResult]:
        self.calls.append(SearchCall(query, lang, max_results, timeout_s))
        if self.error is not None:
            raise self.error
        return self.results[:max_results]


class FakeAppCatalog:
    def __init__(self, apps: Sequence[AppEntry] = ()) -> None:
        self.apps = list(apps)
        self.refresh_count = 0

    def list_apps(self) -> tuple[AppEntry, ...]:
        return tuple(self.apps)

    def refresh(self) -> None:
        self.refresh_count += 1


class FakeLauncher:
    """Records launches. With a catalog, only entries listed in it may be launched."""

    def __init__(self, catalog: FakeAppCatalog | None = None) -> None:
        self.catalog = catalog
        self.launched: list[AppEntry] = []

    def launch(self, app: AppEntry) -> None:
        if self.catalog is not None and app not in self.catalog.list_apps():
            raise ToolError(f"app not in catalog: {app.name}")
        self.launched.append(app)


class FakeBrowser:
    """Records URLs; like the real opener it accepts only http/https (``ValueError``)."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def open_url(self, url: str) -> None:
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme not in ("http", "https"):
            raise ValueError(f"only http/https URLs are allowed: {url!r}")
        self.urls.append(url)


class FakeConfirmer:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def confirm(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.answer


class FakeClock:
    """Manual clock (a ``Clock``: call it for the current time); moves only via ``advance()``."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self._now += seconds


class FakeTool:
    """Returns ``result``, or raises ``error`` when given (e.g. ``ToolError``)."""

    def __init__(
        self,
        name: str = "fake_tool",
        result: ToolResult | None = None,
        safety: SafetyLevel = SafetyLevel.SAFE,
        description: str = "A fake tool.",
        parameters: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters if parameters is not None else {"type": "object"}
        self.safety = safety
        self.result = result or ToolResult(tool=name, ok=True, summary="ok")
        self.error = error
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append((copy.deepcopy(args), ctx))
        if self.error is not None:
            raise self.error
        return self.result
