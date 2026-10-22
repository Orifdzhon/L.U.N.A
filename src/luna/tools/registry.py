"""ToolRegistry: the single gate every tool call passes through (ARCHITECTURE sections 3, 7, 9).

``execute`` never raises for ordinary failures: unknown tools, bad arguments, refused
confirmations and tool failures all come back as ``ToolResult(ok=False)``. Every call,
including refused and failed ones (and ones interrupted by ``KeyboardInterrupt``), writes
exactly one :class:`ActionRecord`; ``BaseException`` is re-raised after the record is written.

Confirmation: for SENSITIVE/DANGEROUS tools ``ctx.confirmer.confirm`` receives the full tool
name and the full JSON arguments. Only a return value of exactly ``True`` is approval. A
``Confirmer`` implementation MUST return ``False`` on timeout, cancellation or any doubt.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from luna.core.errors import ToolError
from luna.llm.base import ToolCall, ToolSchema
from luna.logs.action_log import ActionLog, ActionRecord
from luna.logs.redact import redact_text
from luna.tools.base import SafetyLevel, Tool, ToolContext, ToolResult
from luna.tools.schema import SchemaError, check_schema, validate_args

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 200  # summaries we generate from errors
MAX_LOG_TEXT_CHARS = 500  # summary / user text written to the action log
MAX_ARGS_DEPTH = 8
MAX_ARGS_BYTES = 16 * 1024
MAX_PROMPT_CHARS = 2000
_MAX_ERRORS_SHOWN = 3
_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_TRUNC = "...[truncated]"


def _short(text: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = " ".join(redact_text(str(text)).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _clip(text: Any, limit: int = MAX_LOG_TEXT_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + _TRUNC


def _log_summary(result: ToolResult) -> str:
    """Summary for the action log; untrusted web text is never written to disk."""
    if result.untrusted:
        return f"[untrusted web content omitted, {len(str(result.summary))} chars]"
    return _clip(result.summary)


@dataclass(frozen=True)
class _Entry:
    """Immutable snapshot taken at registration; later mutation of the tool changes nothing."""

    name: str
    description: str
    safety: SafetyLevel
    schema: dict[str, Any]
    tool: Tool


def _escape_unsafe(text: str) -> str:
    """Backslash-escape control, format (bidi) and line/paragraph separator characters."""
    out = []
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            cp = ord(ch)
            out.append(f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}")
        else:
            out.append(ch)
    return "".join(out)


def _depth_ok(value: Any, depth: int = 0) -> bool:
    if depth > MAX_ARGS_DEPTH:
        return False
    if isinstance(value, dict):
        return all(isinstance(k, str) and _depth_ok(v, depth + 1) for k, v in value.items())
    if isinstance(value, list | tuple):
        return all(_depth_ok(v, depth + 1) for v in value)
    return True


def _check_args_shape(args: Any) -> str | None:
    """Return an error message when ``args`` is not a small, JSON-plain dict."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    if not _depth_ok(args):
        return f"arguments nested deeper than {MAX_ARGS_DEPTH} levels or non-string keys"
    try:
        size = len(json.dumps(args, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return "arguments are not JSON-serializable"
    if size > MAX_ARGS_BYTES:
        return f"arguments larger than {MAX_ARGS_BYTES} bytes"
    return None


class ToolRegistry:
    def __init__(
        self,
        action_log: ActionLog,
        *,
        allow_dangerous: bool = False,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = action_log
        self._allow_dangerous = allow_dangerous
        self._timer = timer
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ToolError(f"invalid tool name {name!r}: expected [a-zA-Z0-9_-]{{1,64}}")
        safety = getattr(tool, "safety", None)
        if not isinstance(safety, SafetyLevel):
            raise ToolError(f"tool {name!r}: safety must be a SafetyLevel, got {safety!r}")
        if safety is SafetyLevel.DANGEROUS and not self._allow_dangerous:
            raise ToolError(f"refusing to register DANGEROUS tool {name!r}")
        description = getattr(tool, "description", None)
        if not isinstance(description, str):
            raise ToolError(f"tool {name!r}: description must be a string")
        try:
            schema = copy.deepcopy(tool.parameters)
            check_schema(schema, f"{name}.parameters")
        except (SchemaError, RecursionError, TypeError, AttributeError) as exc:
            raise ToolError(f"tool {name!r}: invalid parameters schema: {exc}") from exc
        entry = _Entry(name, description, safety, schema, tool)
        with self._lock:
            if name in self._entries:
                raise ToolError(f"tool {name!r} is already registered")
            self._entries[name] = entry

    def schemas(self) -> list[ToolSchema]:
        with self._lock:
            entries = list(self._entries.values())
        return [ToolSchema(e.name, e.description, copy.deepcopy(e.schema)) for e in entries]

    def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        start = self._timer()
        name = call.name if isinstance(call.name, str) else repr(call.name)
        result = ToolResult(_short(name, 64), False, "internal error")
        state: dict[str, Any] = {"entry": None, "confirmed": None}
        try:
            result = self._process(call, ctx, name, state)
        except Exception:
            logger.exception("unexpected error while executing tool call %s", _short(name, 64))
        finally:
            # Also runs for KeyboardInterrupt & co.: the record is written, then it propagates.
            self._record(call, ctx, name, state, result, start)
        return result

    def _process(
        self, call: ToolCall, ctx: ToolContext, name: str, state: dict[str, Any]
    ) -> ToolResult:
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            return ToolResult(_short(name, 64), False, f"unknown tool: {_short(name, 64)}")
        state["entry"] = entry

        args: Any = call.arguments
        problem = _check_args_shape(args)
        if problem is None:
            args = copy.deepcopy(args)  # validated, shown and run: always this one snapshot
            try:
                errors = validate_args(args, entry.schema)
            except (OverflowError, RecursionError, TypeError, ValueError):
                errors = ["arguments could not be validated"]
            if errors:
                problem = "; ".join(errors[:_MAX_ERRORS_SHOWN])
        if problem is not None:
            return ToolResult(entry.name, False, _short(f"invalid arguments: {problem}"))

        if entry.safety is not SafetyLevel.SAFE:
            prompt = (
                f"Run tool {entry.name} ({entry.safety.value}) with arguments: "
                f"{_escape_unsafe(json.dumps(args, ensure_ascii=False, sort_keys=True))}"
            )
            if len(prompt) > MAX_PROMPT_CHARS:
                state["confirmed"] = False
                return ToolResult(entry.name, False, "arguments too large to confirm")
            try:
                approved = ctx.confirmer.confirm(prompt) is True
            except Exception:
                logger.exception("confirmation for tool %s failed", entry.name)
                approved = False
            state["confirmed"] = approved
            if not approved:
                return ToolResult(entry.name, False, "not confirmed by the user")

        try:
            result = entry.tool.run(args, ctx)
        except ToolError as exc:
            logger.warning("tool %s failed: %s", entry.name, redact_text(str(exc)))
            return ToolResult(entry.name, False, _short(f"tool failed: {exc}"))
        except Exception as exc:
            logger.exception("tool %s raised %s", entry.name, type(exc).__name__)
            return ToolResult(entry.name, False, f"tool failed unexpectedly ({type(exc).__name__})")
        if not isinstance(result, ToolResult):
            logger.error("tool %s returned %s instead of ToolResult", entry.name, type(result))
            return ToolResult(entry.name, False, "tool returned an invalid result")
        return result

    def _record(
        self,
        call: ToolCall,
        ctx: ToolContext,
        name: str,
        state: dict[str, Any],
        result: ToolResult,
        start: float,
    ) -> None:
        try:
            entry: _Entry | None = state["entry"]
            args = call.arguments if isinstance(call.arguments, dict) else {}
            try:
                args = json.loads(json.dumps(args))
                if len(json.dumps(args)) > MAX_ARGS_BYTES:
                    args = {"_truncated": True}
            except (TypeError, ValueError, RecursionError):
                args = {"_unserializable": True}
            record = ActionRecord(
                request_id=_clip(getattr(ctx, "request_id", ""), 100),
                source=_clip(getattr(ctx, "source", ""), 20),
                text=_clip(getattr(ctx, "user_text", "")),
                tool=_clip(name, 100),
                args=args,
                safety=entry.safety.value if entry else "unknown",
                confirmed=state["confirmed"],
                ok=bool(result.ok),
                summary=_log_summary(result),
                duration_ms=max(0, round((self._timer() - start) * 1000)),
                provider=getattr(ctx, "provider", None),
            )
            if not self._log.write(record):
                logger.warning("action log write failed for tool %s", _short(name, 64))
        except Exception:
            logger.exception("could not write the action record")
