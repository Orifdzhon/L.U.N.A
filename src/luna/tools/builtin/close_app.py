"""``close_app``: close a running app by a (spoken) name, gracefully (ADR 0011, feature A).

SAFE: no confirmation for a normal close (owner decision). The app gets ``WM_CLOSE`` like the
X button of its window; nothing is killed unless the user explicitly agreed to it:

* ``force=true`` is accepted only if an earlier ``close_app`` reported the SAME app as
  not responding / hidden in the tray (a pending offer), less than ``force_window_s`` ago, and
  the call belongs to a LATER request than the offer (the user answered in a new turn).
  Otherwise a normal close is attempted and ``reason="force_not_offered"`` is reported.
* the tool only acts on ``RunningApp`` objects it listed itself in the same call; the model's
  text is only used to pick among them; protected processes are never listed.

Results: ``data["reason"]`` is one of ``closed, prompted, hidden, not_responding, not_running,
no_match, ambiguous (+candidates), protected, force_not_offered, close_failed,
unsupported_platform, unsupported_request, invalid_name``. Summaries contain the app's display
name only, never window titles.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from luna.config.settings import AppsSettings
from luna.core.errors import ProviderUnavailable, ToolError
from luna.platform.base import AppCatalog, CloseOutcome, ProtectedApp, RunningApp, RunningApps
from luna.tools.app_matching import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    MAX_NAME_CHARS,
    MatchStatus,
    merge_aliases,
)
from luna.tools.base import SafetyLevel, ToolContext, ToolResult
from luna.tools.running_match import match_running

NAME = "close_app"


@dataclass(frozen=True)
class PendingForce:
    app_key: str
    name: str
    request_id: str
    at: float


class CloseAppTool:
    name = NAME
    description = (
        "Close a running application by name (e.g. 'Telegram', 'хром', 'блокнот'), the way its "
        "window's close button does. One app at a time. Use force=true ONLY after the user "
        "explicitly agreed to force-close an app this tool reported as not responding."
    )
    parameters: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Application name as the user said it.",
                "minLength": 1,
                "maxLength": MAX_NAME_CHARS,
            },
            "force": {
                "type": "boolean",
                "description": "Force-close after the user agreed (default false).",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    safety = SafetyLevel.SAFE

    def __init__(
        self,
        running: RunningApps,
        catalog: AppCatalog,
        app_aliases: Mapping[str, Any] | None = None,
        *,
        conf: AppsSettings | None = None,
        threshold: int = DEFAULT_THRESHOLD,
        margin: int = DEFAULT_MARGIN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._running = running
        self._catalog = catalog
        self._aliases = merge_aliases(app_aliases)
        self._conf = conf or AppsSettings()
        self._threshold = threshold
        self._margin = margin
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: PendingForce | None = None

    # --- entry point ------------------------------------------------------------------------

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return self._fail("name must be a non-empty string", "invalid_name")
        force = args.get("force") is True
        if getattr(self._running, "supported", True) is False:
            return self._fail(
                "Closing apps is not supported on this system", "unsupported_platform"
            )
        try:
            running = tuple(self._running.list_running())
            entries = tuple(self._catalog.list_apps())
        except ProviderUnavailable:
            entries, running = (), ()
        except ToolError as exc:
            return self._fail(f"Could not list running apps: {exc}", "close_failed")
        found = match_running(
            name,
            running,
            entries,
            self._aliases,
            self._running.identity_matches,
            threshold=self._threshold,
            margin=self._margin,
        )
        if found.status is MatchStatus.UNSUPPORTED:
            self._take_pending()
            return self._fail("Only closing one app by name is supported", "unsupported_request")
        if found.status is MatchStatus.AMBIGUOUS:
            self._take_pending()
            names = [(found.names or {}).get(a.key, a.name) for a in found.candidates]
            return ToolResult(
                NAME,
                False,
                f"Several running apps match: {', '.join(names)}. Which one?",
                {"reason": "ambiguous", "candidates": names},
            )
        if found.status is not MatchStatus.MATCH or found.app is None:
            self._take_pending()
            if found.entry is not None:
                return self._fail(f"{found.entry.name} is not running", "not_running")
            return self._fail("No running app matches that name", "no_match")
        app = found.app
        display = (found.names or {}).get(app.key, app.name)
        if force and self._force_allowed(app, ctx):
            return self._terminate(app, display)
        self._take_pending()
        result = self._close(app, display, ctx)
        if force:
            refused = f"Force-close of {display} was not offered, so I tried a normal close. "
            return ToolResult(
                NAME,
                False,
                refused + result.summary,
                {**result.data, "reason": "force_not_offered", "outcome": result.data["reason"]},
            )
        return result

    # --- pending force offer ------------------------------------------------------------------

    def _take_pending(self) -> PendingForce | None:
        with self._lock:
            pending, self._pending = self._pending, None
        return pending

    def _offer(self, app: RunningApp, display: str, ctx: ToolContext) -> None:
        with self._lock:
            self._pending = PendingForce(app.key, display, ctx.request_id, self._clock())

    def _force_allowed(self, app: RunningApp, ctx: ToolContext) -> bool:
        """Consume the pending offer; True only for the same app, in time, in a later request."""
        pending = self._take_pending()
        return (
            pending is not None
            and pending.app_key == app.key
            and self._clock() - pending.at <= self._conf.force_window_s
            and pending.request_id != ctx.request_id
        )

    # --- actions ------------------------------------------------------------------------------

    def _terminate(self, app: RunningApp, display: str) -> ToolResult:
        try:
            self._running.terminate(app)
        except ProtectedApp:
            return self._fail(f"{display} is protected and cannot be closed", "protected")
        except ToolError as exc:
            return self._fail(f"Could not force-close {display}: {exc}", "close_failed")
        return ToolResult(
            NAME,
            True,
            f"Force-closed {display}",
            {"reason": "closed", "app": display, "forced": True},
        )

    def _close(self, app: RunningApp, display: str, ctx: ToolContext) -> ToolResult:
        try:
            outcome = self._running.close(app, wait_s=self._conf.close_wait_s)
        except ProtectedApp:
            return self._fail(f"{display} is protected and cannot be closed", "protected")
        except ToolError as exc:
            return self._fail(f"Could not close {display}: {exc}", "close_failed")
        data = {"reason": outcome.value, "app": display}
        if outcome is CloseOutcome.CLOSED:
            return ToolResult(NAME, True, f"Closed {display}", data)
        if outcome is CloseOutcome.PROMPTED:
            return ToolResult(
                NAME, True, f"{display} is asking the user something before it closes", data
            )
        if outcome is CloseOutcome.NOT_RUNNING:
            return ToolResult(NAME, False, f"{display} is not running", data)
        self._offer(app, display, ctx)
        if outcome is CloseOutcome.HIDDEN:
            summary = (
                f"{display} closed its window but keeps running in the tray. Ask the user whether "
                "to close it completely; unsaved data will be lost."
            )
            return ToolResult(NAME, True, summary, data)
        summary = (
            f"{display} is not responding. Ask the user whether to force-close it; "
            "unsaved data will be lost."
        )
        return ToolResult(NAME, False, summary, data)

    @staticmethod
    def _fail(summary: str, reason: str) -> ToolResult:
        return ToolResult(NAME, False, summary, {"reason": reason})
