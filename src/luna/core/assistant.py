# ruff: noqa: RUF001
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
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Collection, Sequence
from typing import Protocol

from luna.core.errors import (
    Abandoned,
    EmptyResponse,
    ProviderError,
    ProviderRequestError,
    ToolUseRejected,
)
from luna.core.models import Reply, Request
from luna.core.prompts import (
    NO_TOOLS_NOTE,
    SEARCH_TOOL,
    TOOL_RESULTS_HEADER,
    UNTRUSTED_MARKER,
    build_answer_prompt,
    build_system_prompt,
    fallback_text,
)
from luna.core.router import clean_query, detect_lang
from luna.llm.base import ChatResponse, Message, ToolCall, ToolSchema
from luna.logs.redact import redact_text
from luna.tools.base import ToolContext, ToolResult
from luna.tools.builtin.datetime import current_datetime_context
from luna.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15.0  # overall deadline for one answer(); below the Orchestrator's 20 s
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
        exclude: Collection[str] = (),
    ) -> ChatResponse: ...


_NO_INTERNET_RE = re.compile(
    r"(нет|не\s+имею|не\s+могу\s+получить|отсутствует)\s+(у\s+меня\s+)?"
    r"(доступа\s+к\s+(интернет|актуальн|реальн|данн|текущ)|прямого\s+доступа)"
    r"|не\s+(имею|могу)\s+(доступа|получить\s+доступ)"
    r"|не\s+подключ[её]н\s+к\s+интернет"
    r"|(don't|do\s+not|can't|cannot|can\s?not|unable\s+to)\s+(have\s+)?"
    r"(access|browse|connect)\s+(to\s+)?(the\s+)?"
    r"(internet|real[- ]time|current|live|up[- ]to[- ]date)"
    r"|no\s+(access|internet\s+access)\s+to\s+(the\s+)?(internet|real[- ]time|current|live)"
    r"|(real[- ]time|live|up[- ]to[- ]date)\s+(information|data)\s+(is\s+)?(not\s+)?"
    r"(available|accessible)\s+to\s+me"
    r"|as\s+an\s+ai[^.]*(no|don't\s+have)[^.]*(internet|real[- ]time)",
    re.IGNORECASE,
)


def _claims_no_internet(text: str | None) -> bool:
    """A reply that says the assistant has no internet / real-time access."""
    return bool(text) and _NO_INTERNET_RE.search(text or "") is not None


class _DenyConfirmer:
    def confirm(self, prompt: str) -> bool:
        return False


def _results_message(results: Sequence[ToolResult]) -> Message:
    """One system message with this turn's tool summaries (never ``ToolResult.data``)."""
    lines = [TOOL_RESULTS_HEADER]
    for r in results:
        lines.append(r.summary if r.untrusted else f"- {r.tool}: {r.summary}")
    return Message("system", "\n".join(lines))


_PSEUDO_CALL_RE = re.compile(
    r"<\s*/?\s*(?:search|tool|function|query|nocomment)\b"
    r"|<\s*/?\s*(?:search_result|web_search_results|output)\b"  # our own wrapper markup
    r"|^\s*\|\s"  # the '| ' quote prefix of wrapped web lines
    r"|\{\s*\"name\"\s*:|\bweb_search\s*\(",
    re.IGNORECASE | re.MULTILINE,
)


def _looks_like_tool_call(text: str | None) -> bool:
    """A tools-free reply that is really a tool/search invocation written as text."""
    return bool(text) and _PSEUDO_CALL_RE.search(text or "") is not None


def _collapse(
    messages: list[Message], base: int, results: Sequence[ToolResult], lang: str, now: str
) -> None:
    """Prepare the tools-free follow-up: answer-only system prompt + one results message."""
    messages[0] = Message("system", build_answer_prompt(lang, now))
    messages[base:] = [_results_message(results)]


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Abandoned


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
        self._on_clear: list[Callable[[], None]] = []

    def add_clear_hook(self, hook: Callable[[], None]) -> None:
        """Call ``hook`` whenever the dialogue is cleared (e.g. to forget found files)."""
        self._on_clear.append(hook)

    def clear_history(self) -> None:
        """Forget the dialogue context (the CLI ``/clear`` command)."""
        with self._lock:
            self._history.clear()
        for hook in self._on_clear:
            try:
                hook()
            except Exception:
                logger.exception("clear hook failed")

    def answer(
        self,
        request: Request,
        *,
        force_search: bool = False,
        cancel: threading.Event | None = None,
    ) -> Reply:
        """Answer ``request``. If ``cancel`` gets set the turn stops at the next checkpoint
        (before/after an LLM call, before a tool, before the history write) with no side effects."""
        lang = detect_lang(request.text, request.lang)
        try:
            reply, web = self._run(request, lang, force_search, cancel)
        except Abandoned:
            return Reply(fallback_text("cancelled", lang), lang)
        except ProviderError as exc:
            logger.warning(
                "LLM unavailable: %s: %s", type(exc).__name__, redact_text(str(exc))[:300]
            )
            return Reply(fallback_text("unavailable", lang), lang)
        except _GaveUp:
            return Reply(fallback_text("gave_up", lang), lang)
        if self._history.maxlen:
            stored = reply.text
            if web:
                stored = UNTRUSTED_MARKER + stored[:MAX_WEB_HISTORY_CHARS]
            with self._lock:
                if cancel is not None and cancel.is_set():
                    return Reply(fallback_text("cancelled", lang), lang)
                self._history.append((request.text, stored))
                while len(self._history) > 1 and (
                    sum(len(u) + len(a) for u, a in self._history) > MAX_HISTORY_CHARS
                ):
                    self._history.popleft()
        return reply

    def _run(
        self, request: Request, lang: str, force_search: bool, cancel: threading.Event | None
    ) -> tuple[Reply, bool]:
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
            response = self._chat(messages, tools, deadline, request, cancel)
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
                    result = self._execute(call, request, response.provider, cancel)
                    actions.append(result)
                    web = web or result.untrusted
                    searched = True
                    tools_off = True
                    _collapse(messages, base, actions, lang, now)
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
                result = self._execute(call, request, response.provider, cancel)
                actions.append(result)
                searched = searched or call.name == SEARCH_TOOL
                tools_off = tools_off or result.untrusted
                web = web or result.untrusted
                messages.append(Message("tool", result.summary, tool_call_id=call.id))
            if tools_off:
                # Tools-free follow-up: no assistant tool_calls / tool messages (some providers
                # reject or hallucinate tools then); the results go in one system message.
                _collapse(messages, base, actions, lang, now)

    def _chat(
        self,
        messages: list[Message],
        tools: Sequence[ToolSchema],
        deadline: float,
        request: Request,
        cancel: threading.Event | None,
    ) -> ChatResponse:
        def send(
            msgs: list[Message], offered: Sequence[ToolSchema], skip: Collection[str] = ()
        ) -> ChatResponse:
            _check(cancel)
            remaining = deadline - self._timer()
            if remaining <= 0:
                raise _GaveUp
            extra = {"exclude": skip} if skip else {}  # only LLMChain understands ``exclude``
            response = self._llm.chat(
                msgs, offered, timeout_s=remaining, request_id=request.request_id, **extra
            )
            _check(cancel)
            return response

        if tools:
            return send(messages, tools)
        # Tools-free call: the model may still try a tool (provider 400), return nothing, or
        # write a pseudo tool call as text. Retry once in plain text, then give up gracefully.
        try:
            response = send(messages, ())
            if _claims_no_internet(response.text):
                return self._retry_elsewhere(send, messages, response)
            if not _looks_like_tool_call(response.text):
                return response
            logger.warning("tools-free reply looked like a tool call; retrying")
        except (ToolUseRejected, EmptyResponse):
            logger.warning("tools-free call rejected or empty; retrying")
        try:
            response = send([*messages, Message("system", NO_TOOLS_NOTE)], ())
        except ProviderRequestError:
            raise _GaveUp from None
        if _looks_like_tool_call(response.text):
            raise _GaveUp
        return response

    @staticmethod
    def _retry_elsewhere(
        send: Callable[..., ChatResponse], messages: list[Message], first: ChatResponse
    ) -> ChatResponse:
        """The model ignored the fetched results ("no internet"): ask the next provider once."""
        logger.warning("tools-free reply claimed no internet access; trying the next provider")
        skip = {first.provider} if first.provider else set()
        try:
            second = send(messages, (), skip) if skip else None
        except ProviderError:
            raise _GaveUp from None
        if second is None or _claims_no_internet(second.text) or _looks_like_tool_call(second.text):
            raise _GaveUp
        return second

    def _execute(
        self,
        call: ToolCall,
        request: Request,
        provider: str | None,
        cancel: threading.Event | None,
    ) -> ToolResult:
        _check(cancel)
        ctx = ToolContext(
            request_id=request.request_id,
            source=request.source,
            confirmer=self._confirmer,
            user_text=request.text,
            provider=provider,
        )
        return self._registry.execute(call, ctx)
