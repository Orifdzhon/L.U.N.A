"""Injectable time sources."""

from __future__ import annotations

from collections.abc import Callable

# Monotonic seconds (same signature as ``time.monotonic``); production code takes a ``Clock``
# so tests can inject a manual one (e.g. for LLMChain cooldowns).
Clock = Callable[[], float]

# Wall-clock time: seconds since the Unix epoch, e.g. ``time.time`` (used by ``get_datetime``).
WallClock = Callable[[], float]
