"""``open_app``: launch an installed app from the catalog by a (spoken) name (F4).

Only ``AppEntry`` objects returned by the ``AppCatalog`` are ever passed to the launcher
(ARCHITECTURE section 9); the model-supplied text is only used to pick among them.
Results: ``ok=True`` launched; ``ok=False`` with ``data["reason"]`` one of ``no_match``,
``ambiguous`` (plus ``candidates``), ``unsupported_request``, ``catalog_unavailable``,
``open_failed``, ``invalid_name``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from typing import Any

from luna.core.errors import ProviderUnavailable, ToolError
from luna.platform.base import AppCatalog, AppEntry, AppLauncher
from luna.tools.app_matching import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    MAX_NAME_CHARS,
    MatchResult,
    MatchStatus,
    match_app,
    merge_aliases,
)
from luna.tools.base import SafetyLevel, ToolContext, ToolResult

NAME = "open_app"
REFRESH_MIN_INTERVAL_S = 300.0  # at most one rescan on a miss per 5 minutes


class OpenAppTool:
    name = NAME
    description = (
        "Open an installed application by name (e.g. 'Telegram', 'хром', 'калькулятор'). "
        "Only apps from the Start menu can be opened."
    )
    parameters: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Application name as the user said it.",
                "minLength": 1,
                "maxLength": MAX_NAME_CHARS,
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    safety = SafetyLevel.SAFE

    def __init__(
        self,
        catalog: AppCatalog,
        launcher: AppLauncher,
        app_aliases: Mapping[str, Any] | None = None,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        margin: int = DEFAULT_MARGIN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``app_aliases``/``threshold``/``margin`` come from ``settings.router``."""
        self._catalog = catalog
        self._launcher = launcher
        self._aliases = merge_aliases(app_aliases)
        self._threshold = threshold
        self._margin = margin
        self._clock = clock
        self._last_refresh: float | None = None

    def _match(self, name: str) -> MatchResult:
        apps = self._catalog.list_apps()
        return match_app(name, apps, self._aliases, threshold=self._threshold, margin=self._margin)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return self._fail("name must be a non-empty string", "invalid_name")
        try:
            result = self._match(name)
            if result.status is MatchStatus.NONE and self._refresh_allowed():
                # The app may have been installed after the scan. The Windows rescan can
                # block for up to its timeout (20 s), hence the rate limit.
                with contextlib.suppress(ProviderUnavailable):
                    self._catalog.refresh()
                result = self._match(name)
        except ProviderUnavailable:
            return self._fail("The list of installed apps is not available", "catalog_unavailable")

        if result.status is MatchStatus.UNSUPPORTED:
            return self._fail("Only opening one app by name is supported", "unsupported_request")
        if result.status is MatchStatus.NONE:
            return self._fail("No installed app matches that name", "no_match")
        if result.status is MatchStatus.AMBIGUOUS:
            names = [e.name for e in result.candidates]
            return ToolResult(
                NAME,
                False,
                f"Several apps match: {', '.join(names)}. Which one?",
                {"reason": "ambiguous", "candidates": names},
            )
        entry = result.entry
        assert entry is not None  # MATCH always carries an entry
        return self._launch(entry)

    def _refresh_allowed(self) -> bool:
        now = self._clock()
        if self._last_refresh is not None and now - self._last_refresh < REFRESH_MIN_INTERVAL_S:
            return False
        self._last_refresh = now
        return True

    def _launch(self, entry: AppEntry) -> ToolResult:
        try:
            self._launcher.launch(entry)
        except ToolError as exc:
            return self._fail(f"Could not open {entry.name}: {exc}", "open_failed")
        return ToolResult(NAME, True, f"Opened {entry.name}", {"app": entry.name})

    @staticmethod
    def _fail(summary: str, reason: str) -> ToolResult:
        return ToolResult(NAME, False, summary, {"reason": reason})
