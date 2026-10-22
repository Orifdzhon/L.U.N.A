"""``get_datetime``: the real local date and time (ADR 0006 decision 4, ARCHITECTURE section 2).

The module is named like the stdlib one on purpose (path fixed by ARCHITECTURE). It is safe
because imports are absolute: ``import datetime`` here still resolves to the stdlib module.
The clock is a ``luna.core.clock.WallClock`` returning wall-clock epoch seconds (``time.time`` in
production, a ``FakeClock`` started at an epoch value in tests). Without an explicit ``tz`` the
system local zone is used, so no ``tzdata`` dependency is needed.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from luna.core.clock import WallClock
from luna.tools.base import SafetyLevel, ToolContext, ToolResult

NAME = "get_datetime"

# Fixed English names: ``strftime("%A")`` would depend on the process locale.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _offset(moment: dt.datetime) -> str:
    total = int((moment.utcoffset() or dt.timedelta(0)).total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _info(clock: WallClock, tz: dt.tzinfo | None) -> dict[str, Any]:
    utc = dt.datetime.fromtimestamp(clock(), tz=dt.UTC)
    moment = utc.astimezone(tz).replace(microsecond=0)  # tz=None: system local zone
    return {
        "iso": moment.isoformat(),
        "date": moment.date().isoformat(),
        "time": moment.time().isoformat(),
        "weekday": _WEEKDAYS[moment.weekday()],
        "utc_offset": _offset(moment),
        "timezone": moment.tzname() or "",
    }


def current_datetime_context(clock: WallClock = time.time, tz: dt.tzinfo | None = None) -> str:
    """One line for the system prompt (used by #19)."""
    info = _info(clock, tz)
    return (
        f"Current local date and time: {info['date']} {info['time']} "
        f"(UTC{info['utc_offset']}), {info['weekday']}."
    )


class GetDatetimeTool:
    name = NAME
    description = (
        "Get the current local date, time, weekday and UTC offset. "
        "Use it for questions about today, now, the current day or time."
    )
    parameters: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    safety = SafetyLevel.SAFE

    def __init__(self, clock: WallClock = time.time, tz: dt.tzinfo | None = None) -> None:
        self._clock = clock
        self._tz = tz

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        info = _info(self._clock, self._tz)
        summary = f"{info['weekday']}, {info['date']} {info['time']} (UTC{info['utc_offset']})"
        return ToolResult(NAME, True, summary, info)
