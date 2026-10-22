"""ToolRegistry: the single gate every tool call passes through (ARCHITECTURE sections 3, 7, 9).

``execute`` never raises: unknown tools, bad arguments, refused confirmations and tool
failures all come back as ``ToolResult(ok=False)``, and every call - including refused and
failed ones - writes exactly one :class:`ActionRecord`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from luna.core.errors import ToolError
from luna.llm.base import ToolCall, ToolSchema
from luna.logs.action_log import ActionLog, ActionRecord
from luna.logs.redact import redact_text
from luna.tools.base import SafetyLevel, Tool, ToolContext, ToolResult
from luna.tools.schema import validate_args

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 200
_MAX_ERRORS_SHOWN = 3


def _short(text: str) -> str:
    text = " ".join(redact_text(str(text)).split())
    return text if len(text) <= MAX_SUMMARY_CHARS else text[: MAX_SUMMARY_CHARS - 3] + "..."


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
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.safety is SafetyLevel.DANGEROUS and not self._allow_dangerous:
            raise ToolError(f"refusing to register DANGEROUS tool {tool.name!r}")
        if tool.name in self._tools:
            raise ToolError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def schemas(self) -> list[ToolSchema]:
        return [ToolSchema(t.name, t.description, t.parameters) for t in self._tools.values()]

    def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        start = self._timer()
        tool = self._tools.get(call.name)
        confirmed: bool | None = None
        if tool is None:
            result = ToolResult(call.name, False, f"unknown tool: {_short(call.name)}")
        else:
            result, confirmed = self._run_checked(tool, call, ctx)
        duration_ms = max(0, round((self._timer() - start) * 1000))
        self._record(call, ctx, tool, result, confirmed, duration_ms)
        return result

    def _run_checked(
        self, tool: Tool, call: ToolCall, ctx: ToolContext
    ) -> tuple[ToolResult, bool | None]:
        args: Any = call.arguments
        errors = validate_args(args, tool.parameters)
        if errors:
            shown = "; ".join(errors[:_MAX_ERRORS_SHOWN])
            return ToolResult(tool.name, False, _short(f"invalid arguments: {shown}")), None

        confirmed: bool | None = None
        if tool.safety is not SafetyLevel.SAFE:
            confirmed = self._confirm(tool, args, ctx)
            if not confirmed:
                return ToolResult(tool.name, False, "not confirmed by the user"), False

        try:
            result = tool.run(dict(args), ctx)
        except ToolError as exc:
            logger.warning("tool %s failed: %s", tool.name, redact_text(str(exc)))
            return ToolResult(tool.name, False, _short(f"tool failed: {exc}")), confirmed
        except Exception as exc:
            logger.exception("tool %s raised %s", tool.name, type(exc).__name__)
            summary = f"tool failed unexpectedly ({type(exc).__name__})"
            return ToolResult(tool.name, False, summary), confirmed
        if not isinstance(result, ToolResult):
            logger.error("tool %s returned %s instead of ToolResult", tool.name, type(result))
            return ToolResult(tool.name, False, "tool returned an invalid result"), confirmed
        return result, confirmed

    @staticmethod
    def _confirm(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> bool:
        prompt = _short(f"Run {tool.name} ({tool.safety.value}) with {args!r}?")
        try:
            return bool(ctx.confirmer.confirm(prompt))
        except Exception:
            logger.exception("confirmation for tool %s failed", tool.name)
            return False

    def _record(
        self,
        call: ToolCall,
        ctx: ToolContext,
        tool: Tool | None,
        result: ToolResult,
        confirmed: bool | None,
        duration_ms: int,
    ) -> None:
        args = call.arguments if isinstance(call.arguments, dict) else {}
        record = ActionRecord(
            request_id=ctx.request_id,
            source=ctx.source,
            text=ctx.user_text,
            tool=call.name,
            args=args,
            safety=tool.safety.value if tool else "unknown",
            confirmed=confirmed,
            ok=result.ok,
            summary=result.summary,
            duration_ms=duration_ms,
            provider=ctx.provider,
        )
        try:
            if not self._log.write(record):
                logger.warning("action log write failed for tool %s", call.name)
        except Exception:
            logger.exception("action log raised for tool %s", call.name)
