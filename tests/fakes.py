"""Tiny in-memory fakes for the protocols in ``luna.*.base`` (test infrastructure only).

Import convention: ``pytest`` puts ``tests/`` on ``sys.path`` (see ``pythonpath`` in
``pyproject.toml``), so tests do ``from fakes import FakeLLM``.
"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Collection, Sequence
from typing import Any, NamedTuple

from luna.audio.base import AudioClip
from luna.core.errors import ToolError
from luna.llm.base import ChatResponse, Message, ToolSchema
from luna.platform.base import AppEntry
from luna.search.base import SearchResult
from luna.stt.base import Transcript
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
    exclude: frozenset[str] = frozenset()


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
        request_id: str | None = None,
        exclude: Collection[str] = (),
    ) -> ChatResponse:
        self.calls.append(
            LLMCall(
                copy.deepcopy(list(messages)),
                copy.deepcopy(list(tools)),
                timeout_s,
                frozenset(exclude),
            )
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


class FakePlayer:
    """Records clips. With ``block=True``, ``play()`` waits for ``stop()`` or ``finish()``.

    ``stop()`` / ``finish()`` act only on a ``play()`` that is running (like the real
    player; a call while idle is a no-op). ``on_enter`` runs at the very start of
    ``play()``, before the clip counts as playing: tests use it to hold ``play()`` back
    and let a ``stop()`` land in that window.
    """

    def __init__(self, block: bool = False, on_enter: Callable[[], None] | None = None) -> None:
        self.block = block
        self.on_enter = on_enter
        self.played: list[AudioClip] = []
        self.returned = 0  # number of play() calls that have finished
        self.stop_count = 0
        self.closed = False
        self.started = threading.Event()  # set when play() begins (to sync tests)
        self._lock = threading.Lock()
        self._current: threading.Event | None = None  # release flag of the running play()

    def play(self, clip: AudioClip) -> None:
        if self.on_enter is not None:
            self.on_enter()
        release = threading.Event()
        with self._lock:
            self.played.append(clip)
            self._current = release
        self.started.set()
        try:
            if self.block:
                release.wait(timeout=5.0)  # safety net so a broken test cannot hang
        finally:
            with self._lock:
                self._current = None
                self.returned += 1

    def stop(self) -> None:
        with self._lock:
            self.stop_count += 1
            if self._current is not None:
                self._current.set()

    def finish(self) -> None:
        """Let a blocked ``play()`` return as if the clip ended naturally."""
        with self._lock:
            if self._current is not None:
                self._current.set()

    def close(self) -> None:
        self.closed = True


class TTSCall(NamedTuple):
    text: str
    lang: str


class FakeTTS:
    """Returns a short silent clip (or raises ``error``); records every call.

    ``delay`` makes each call take that long; ``max_concurrent`` is the highest number of
    ``synthesize()`` calls that ran at the same time.
    """

    def __init__(
        self, error: Exception | None = None, name: str = "fake", delay: float = 0.0
    ) -> None:
        self.name = name
        self.error = error
        self.delay = delay
        self.calls: list[TTSCall] = []
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()

    def synthesize(self, text: str, lang: str) -> AudioClip:
        with self._lock:
            self.calls.append(TTSCall(text, lang))
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return AudioClip(pcm=b"\x00\x00" * 160, sample_rate=16000)
        finally:
            with self._lock:
                self._active -= 1


# ===========================================================================
# STT fakes (issue #80) - keep this section self-contained
# ===========================================================================


class STTCall(NamedTuple):
    pcm16: bytes
    sample_rate: int
    lang_hint: str | None
    prompt: str | None
    timeout_s: float


class FakeSTT:
    """Replays a scripted list of ``Transcript`` results; an ``Exception`` item is raised.

    Raises ``ScriptExhausted`` (a ``BaseException``) when called more often than scripted.
    """

    def __init__(self, script: Sequence[Transcript | Exception], name: str = "fake") -> None:
        self.name = name
        self._script = list(script)
        self.calls: list[STTCall] = []

    def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = 16_000,
        *,
        lang_hint: str | None = None,
        prompt: str | None = None,
        timeout_s: float,
    ) -> Transcript:
        self.calls.append(STTCall(pcm16, sample_rate, lang_hint, prompt, timeout_s))
        if not self._script:
            raise ScriptExhausted(f"FakeSTT {self.name!r} called more often than scripted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
