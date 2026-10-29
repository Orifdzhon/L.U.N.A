"""Key-related shell helpers without Qt: the key check result and the first-run nudge condition.

The dialog itself (key save, status, Test) lives in ``luna.ui.shell.providers`` (ADR 0011).
"""

from __future__ import annotations

from enum import StrEnum

from luna.config.settings import Settings

TEST_TIMEOUT_S = 10.0


class KeyCheck(StrEnum):
    OK = "OK"
    REJECTED = "Rejected (401/403)"
    NO_CONNECTION = "No connection"
    ERROR = "Unexpected answer"


def has_cloud_llm_key(settings: Settings) -> bool:
    """True when at least one LLM provider that needs a key has one."""
    return any(
        settings.api_key(p.api_key_env) is not None
        for p in settings.llm.providers.values()
        if (p.api_key_env or "").strip()
    )
