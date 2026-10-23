"""Assistant: the LLM tool-calling loop (ARCHITECTURE sections 4.1, 9).

Security invariants:

- once a tool result with ``untrusted=True`` is in the conversation, every later LLM call of the
  turn is made with ``tools=()``: the model can only answer;
- only ``ToolResult.summary`` reaches the prompt, never ``ToolResult.data``;
- tools run only through ``ToolRegistry.execute`` (SAFE tools only: confirmations are refused);
- the loop is bounded by ``max_rounds`` tool rounds and an overall deadline.

Provider failures become a short apology ``Reply``; ``BaseException`` (e.g. a test fake's
``ScriptExhausted``) is never swallowed. Between turns only user/assistant text is kept.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Protocol

from luna.core.errors import ProviderError, ToolUseRejected
from luna.core.models import Reply, Request
from luna.core.prompts import (
    NO_TOOLS_NOTE,
    SEARCH_TOOL,
    TOOL_RESULTS_HEADER,
    WEB_MARKER,
    build_system_prompt,
    fallback_text,
)
from luna.core.router import clean_query, detect_lang
from luna.llm.base import ChatResponse, Message, ToolCall, ToolSchema
from luna.tools.base import ToolContext, ToolResult
from luna.tools.builtin.datetime import current_datetime_context
from luna.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 25.0  # overall deadline for one answer()
MAX_SEARCH_QUERY_CHARS = 200
MAX_WEB_HISTORY_CHARS = 500  # stored length of a web-derived answer
MAX_HISTORY_CHARS = 6000  # total characters kept in the dialog history
_SKIPPED = "skipped: not executed after web content was received"


class _Chat(Protocol):
    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
        request_id: str | None = None,
    ) -> ChatResponse: ...


class _DenyConfirmer:
    def confirm(self, prompt: str) -> bool:
        return False


def _results_message(results: Sequence[ToolResult]) -> Message:
    """One system message with this turn's tool summaries (never ``ToolResult.data``)."""
    lines = [TOOL_RESULTS_HEADER]
    for r in results:
        lines.append(r.summary if r.untrusted else f"- {r.tool}: {r.summary}")
    return Message("system", "\n".join(lines))


class _GaveUp(Exception):
    """Internal: the loop cannot produce an answer (rounds or deadline exhausted)."""


class Assistant:
    def __init__(
        self,
        llm: _Chat,
        registry: ToolRegistry,
        *,
        history_turns: int = 6,
        max_rounds: int = 3,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        now_context: Callable[[], str] = current_datetime_context,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._max_rounds = max(0, max_rounds)
        self._timeout_s = timeout_s
        self._now_context = now_context
        self._timer = timer
        self._history: deque[tuple[str, str]] = deque(maxlen=max(0, history_turns))
        self._confirmer = _DenyConfirmer()
        self._lock = threading.Lock()  # guards _history only; never held during an LLM call

    def answer(self, request: Request, *, force_search: bool = False) -> Reply:
        lang = detect_lang(request.text, request.lang)
        try:
            reply, web = self._run(request, lang, force_search)
        except ProviderError as exc:
            logger.warning("LLM unavailable: %s", type(exc).__name__)
            return Reply(fallback_text("unavailable", lang), lang)
        except _GaveUp:
            return Reply(fallback_text("gave_up", lang), lang)
        if self._history.maxlen:
            stored = reply.text
            if web:
                stored = WEB_MARKER + stored[:MAX_WEB_HISTORY_CHARS]
            with self._lock:
                self._history.append((request.text, stored))
                while len(self._history) > 1 and (
                    sum(len(u) + len(a) for u, a in self._history) > MAX_HISTORY_CHARS
                ):
                    self._history.popleft()
        return reply

    def _run(self, request: Request, lang: str, force_search: bool) -> tuple[Reply, bool]:
        deadline = self._timer() + self._timeout_s
        schemas = self._registry.schemas()
        force = force_search and any(s.name == SEARCH_TOOL for s in schemas)
        try:
            now = self._now_context()
        except Exception:
            logger.exception("date/time context failed")
            now = ""
        messages: list[Message] = [
            Message("system", build_system_prompt(lang, now, force_search=force))
        ]
        with self._lock:
            history = list(self._history)
        for user_text, reply_text in history:
            messages.append(Message("user", user_text))
            messages.append(Message("assistant", reply_text))
        messages.append(Message("user", request.text))

        base = len(messages)  # system + history + the user message
        actions: list[ToolResult] = []
        web = False  # any untrusted content was used this turn
        tools_off = False  # set after untrusted content / a forced search
        searched = False
        rounds = 0
        while True:
            tools: Sequence[ToolSchema] = () if tools_off else schemas
            response = self._chat(messages, tools, deadline, request)
            calls = response.tool_calls if tools else ()
            if not calls:
                if force and not searched:
                    # The model skipped the search: run it ourselves.
                    query = clean_query(request.text)[:MAX_SEARCH_QUERY_CHARS] or request.text
                    call = ToolCall(
                        id=f"assistant-{request.request_id}-search",
                        name=SEARCH_TOOL,
                        arguments={"query": query.strip()},
                    )
                    result = self._execute(call, request, response.provider)
                    actions.append(result)
                    web = web or result.untrusted
                    searched = True
                    tools_off = True
                    messages[base:] = [_results_message(actions)]
                    continue
                text = (response.text or "").strip()
                if not text:
                    raise _GaveUp
                return Reply(text, lang, actions, response.provider), web

            rounds += 1
            if rounds > self._max_rounds:
                raise _GaveUp
            messages.append(Message("assistant", response.text, tuple(calls)))
            for call in calls:
                if tools_off:
                    # Web content already arrived in this batch: do not act any further.
                    messages.append(Message("tool", _SKIPPED, tool_call_id=call.id))
                    continue
                result = self._execute(call, request, response.provider)
                actions.append(result)
                searched = searched or call.name == SEARCH_TOOL
                tools_off = tools_off or result.untrusted
                web = web or result.untrusted
                messages.append(Message("tool", result.summary, tool_call_id=call.id))
            if tools_off:
                # Tools-free follow-up: no assistant tool_calls / tool messages (some providers
                # reject or hallucinate tools then); the results go in one system message.
                messages[base:] = [_results_message(actions)]

    def _chat(
        self,
        messages: list[Message],
        tools: Sequence[ToolSchema],
        deadline: float,
        request: Request,
    ) -> ChatResponse:
        remaining = deadline - self._timer()
        if remaining <= 0:
            raise _GaveUp
        try:
            return self._llm.chat(
                messages, tools, timeout_s=remaining, request_id=request.request_id
            )
        except ToolUseRejected:
            if tools:
                raise
        # The model tried to call a tool on a tools-free call: retry once, in plain text.
        remaining = deadline - self._timer()
        if remaining <= 0:
            raise _GaveUp
        retry = [*messages, Message("system", NO_TOOLS_NOTE)]
        return self._llm.chat(retry, (), timeout_s=remaining, request_id=request.request_id)

    def _execute(self, call: ToolCall, request: Request, provider: str | None) -> ToolResult:
        ctx = ToolContext(
            request_id=request.request_id,
            source=request.source,
            confirmer=self._confirmer,
            user_text=request.text,
            provider=provider,
        )
        return self._registry.execute(call, ctx)
