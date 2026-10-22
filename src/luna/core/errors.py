"""Error hierarchy shared by the core, providers and tools."""

from __future__ import annotations


class LunaError(Exception):
    """Base class for all L.U.N.A. errors."""


class ProviderError(LunaError):
    """A pluggable provider (LLM, STT, TTS, search) failed."""


class RateLimited(ProviderError):
    """The provider asked us to back off.

    ``retry_after_s`` is in seconds; ``None`` means "use the default cooldown".
    """

    def __init__(self, message: str = "rate limited", retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ProviderUnavailable(ProviderError):
    """Network failure, 5xx or timeout."""


class ProviderRequestError(ProviderError):
    """Our fault (HTTP 400, schema error): retried on another provider at most once."""


class ConfigError(LunaError):
    """Invalid or missing configuration."""


class ToolError(LunaError):
    """A tool failed in a way that is not a normal ``ToolResult(ok=False)``."""
