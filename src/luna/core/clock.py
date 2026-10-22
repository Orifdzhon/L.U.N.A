"""Injectable time source."""

from __future__ import annotations

from collections.abc import Callable

# Same signature as ``time.monotonic``; production code takes a ``Clock`` so tests can inject
# a manual one (e.g. for LLMChain cooldowns).
Clock = Callable[[], float]
