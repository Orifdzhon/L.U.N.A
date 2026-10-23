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


class EmptyResponse(ProviderRequestError):
    """The model returned neither text nor tool calls: a request-shape problem, not downtime,
    so the provider is not put on cooldown."""


class ToolUseRejected(ProviderRequestError):
    """The provider rejected the call because the model tried to call a tool that is not offered
    (e.g. Groq ``tool_use_failed`` on a call made with ``tools=()``)."""


class ConfigError(LunaError):
    """Invalid or missing configuration."""


class ToolError(LunaError):
    """A tool failed in a way that is not a normal ``ToolResult(ok=False)``."""
