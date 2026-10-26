"""Tiny in-memory fakes for the protocols in ``luna.*.base`` (test infrastructure only).

Import convention: ``pytest`` puts ``tests/`` on ``sys.path`` (see ``pythonpath`` in
``pyproject.toml``), so tests do ``from fakes import FakeLLM``.
"""

from __future__ import annotations

import copy
import queue
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
from luna.wakeword.base import WAKE_SAMPLE_RATE, WakeEvent


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


# --- microphone capture (#78) -------------------------------------------------------------


class FakeMic:
    """Scripted ``MicSource``: ``read()`` returns the given frames, then None (or raises).

    ``error`` is raised by ``start()``; ``read_error`` by ``read()`` once the frames ran out.
    """

    def __init__(
        self,
        frames: Sequence[bytes] = (),
        error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self._frames = list(frames)
        self.error = error
        self.read_error = read_error
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.closed = False

    def start(self) -> None:
        if self.error is not None:
            raise self.error
        self.started = True
        self.start_calls += 1

    def stop(self) -> None:
        self.started = False
        self.stop_calls += 1

    def read(self, timeout_s: float) -> bytes | None:
        if not self.started:
            return None
        if self._frames:
            return self._frames.pop(0)
        if self.read_error is not None:
            raise self.read_error
        return None

    def close(self) -> None:
        self.closed = True
        self.stop()


# ---------------------------------------------------------------------------------------
# Hotkey fakes (issue #79)
# ---------------------------------------------------------------------------------------


class FakeHotkeyListener:
    """In-memory ``HotkeyListener``: ``press()`` runs the callback synchronously."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.hotkey: str | None = None
        self.running = False
        self._on_press = None

    def start(self, hotkey: str, on_press) -> None:
        if self.error is not None:
            raise self.error
        if self.running:
            raise RuntimeError("fake hotkey listener already running")
        self.hotkey = hotkey
        self._on_press = on_press
        self.running = True

    def stop(self) -> None:
        self.running = False
        self._on_press = None

    def press(self) -> None:
        if not self.running or self._on_press is None:
            raise RuntimeError("fake hotkey listener is not running")
        self._on_press()


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


# ===========================================================================
# Wake-word fakes (issue #104) - keep this section self-contained
# ===========================================================================

WAKE_MARKER = b"WAKE"  # a frame starting with these bytes triggers FakeWakeWord


class FakeWakeWord:
    """Scripted ``WakeWordDetector``.

    Fires on the frame indices in ``trigger_at`` (0-based, counted since construction or the
    last ``reset()``) and, if ``marker`` is true, on any frame that starts with ``WAKE_MARKER``.
    ``end_sample`` of the event is the end of the triggering frame.
    """

    def __init__(
        self,
        trigger_at: Collection[int] = (),
        *,
        keyword: str = "luna",
        confidence: float | None = 0.9,
        frame_samples: int = 480,
        marker: bool = True,
        error: Exception | None = None,
        name: str = "fake",
        end_lag_samples: int = 0,
        report_end: bool = True,
    ) -> None:
        self.name = name
        self.sample_rate = WAKE_SAMPLE_RATE
        self.frame_samples = frame_samples
        self.keyword = keyword
        self.confidence = confidence
        self.marker = marker
        self.error = error
        self.end_lag_samples = end_lag_samples  # keyword "ended" this long before the frame end
        self.report_end = report_end  # False: events carry end_sample=None
        self._trigger_at = frozenset(trigger_at)
        self.frames: list[bytes] = []  # every frame seen (kept across reset)
        self.reset_count = 0
        self.closed = False
        self._index = 0

    def process(self, frame: bytes) -> WakeEvent | None:
        if self.closed:
            raise RuntimeError("FakeWakeWord is closed")
        if self.error is not None:
            raise self.error
        if len(frame) != self.frame_samples * 2:
            raise ValueError(f"expected {self.frame_samples * 2} bytes, got {len(frame)}")
        self.frames.append(frame)
        index = self._index
        self._index += 1
        if index in self._trigger_at or (self.marker and frame.startswith(WAKE_MARKER)):
            end = (index + 1) * self.frame_samples - self.end_lag_samples
            return WakeEvent(self.keyword, self.confidence, end if self.report_end else None)
        return None

    def reset(self) -> None:
        self.reset_count += 1
        self._index = 0

    def close(self) -> None:
        self.closed = True


# ===========================================================================
# Live frame source fakes (issue #107) - keep this section self-contained
# ===========================================================================


class FakeFrameSource:
    """Scripted ``FrameSource`` with a ``close()`` like ``FrameTap`` (records close calls).

    ``read()`` returns the given frames, then None (or raises ``read_error`` once they ran
    out). ``closed`` becomes true after ``close()``; a closed source returns None.
    """

    def __init__(self, frames: Sequence[bytes] = (), read_error: Exception | None = None) -> None:
        self._frames = list(frames)
        self.read_error = read_error
        self.closed = False
        self.close_calls = 0
        self.reads = 0

    def read(self, timeout_s: float) -> bytes | None:
        self.reads += 1
        if self.closed:
            return None
        if self._frames:
            return self._frames.pop(0)
        if self.read_error is not None:
            raise self.read_error
        return None

    def close(self, error: Exception | None = None) -> None:
        self.closed = True
        self.close_calls += 1


# ===========================================================================
# Wake listener fakes (issue #108) - keep this section self-contained
# ===========================================================================


class FakeLiveMic:
    """Blocking ``MicSource`` for threaded tests: frames arrive via ``feed()``.

    ``read()`` waits for a frame (at most ``timeout_s``), returns None when stopped or idle.
    ``fail()`` makes the next ``read()`` raise. ``start()`` raises ``error`` when given.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.closed = False
        self._items: queue.Queue[bytes | Exception | None] = queue.Queue()

    def feed(self, *frames: bytes) -> None:
        for frame in frames:
            self._items.put(frame)

    def fail(self, error: Exception) -> None:
        self._items.put(error)

    def start(self) -> None:
        if self.error is not None:
            raise self.error
        self.started = True
        self.start_calls += 1
        while not self._items.empty():  # a fresh stream
            self._items.get_nowait()

    def stop(self) -> None:
        self.started = False
        self.stop_calls += 1
        self._items.put(None)  # wakes a blocked read()

    def read(self, timeout_s: float) -> bytes | None:
        if not self.started:
            return None
        try:
            item = self._items.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True
        self.stop()


class FakeVAD:
    """``VoiceActivityDetector``: a frame is speech if any byte is non-zero (no warm-up)."""

    def __init__(self, noise_floor: float = 0.0001) -> None:
        self.noise_floor = noise_floor
        self.calls = 0
        self.resets: list[float | None] = []

    def is_speech(self, frame: bytes) -> bool:
        self.calls += 1
        return any(frame)

    def reset(self, noise_floor: float | None = None) -> None:
        self.resets.append(noise_floor)


# --- wake verification (#127) ----------------------------------------------------------------


class FakeVerifier:
    """``WakeVerifier``: scripted result, optional delay, optional exception.

    ``result`` is a ``VerifyResult`` or a list of them (consumed in order, the last one repeats).
    ``calls`` records every ``pcm16`` passed to ``verify()``.
    """

    name = "fake"

    def __init__(
        self,
        result: Any = None,
        *,
        delay: float = 0.0,
        error: BaseException | None = None,
        warm_up_error: BaseException | None = None,
    ) -> None:
        from luna.wakeword.verify import VerifyResult

        if result is None:
            result = VerifyResult(True, "keyword", "луна")
        self._results = list(result) if isinstance(result, (list, tuple)) else [result]
        self.delay = delay
        self.error = error
        self.warm_up_error = warm_up_error
        self.calls: list[bytes] = []
        self.warm_ups = 0
        self.closed = False

    def warm_up(self) -> None:
        self.warm_ups += 1
        if self.warm_up_error is not None:
            raise self.warm_up_error

    def verify(self, pcm16: bytes) -> Any:
        self.calls.append(pcm16)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]

    def close(self) -> None:
        self.closed = True
