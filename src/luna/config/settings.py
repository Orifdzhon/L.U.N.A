"""Application settings: the full slice-0/1 schema (ARCHITECTURE section 6).

API keys are never part of the TOML schema. They are resolved from the environment / ``.env``
by the loader and kept in a private attribute as :class:`~pydantic.SecretStr`, so they never
show up in ``repr()``, ``model_dump()`` or ``model_dump_json()``.
"""

from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The Assistant must give up this many seconds before the Orchestrator's overall timeout.
ASSISTANT_MARGIN_S = 3.0


class AppSettings(_Section):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    history_turns: int = Field(default=6, ge=0)
    request_timeout_s: float = Field(default=20.0, gt=ASSISTANT_MARGIN_S)


class ProviderSettings(_Section):
    """One OpenAI-compatible LLM endpoint."""

    base_url: str
    model: str
    api_key_env: str | None = None  # name of the env variable; None = no key needed (local)
    # Optional per-provider quirks (ADR 0002).
    supports_tool_choice: bool | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)


def _default_providers() -> dict[str, ProviderSettings]:
    return {
        "gemini": ProviderSettings(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-3.5-flash-lite",
            api_key_env="GEMINI_API_KEY",
        ),
        "groq": ProviderSettings(
            base_url="https://api.groq.com/openai/v1",
            model="openai/gpt-oss-120b",
            api_key_env="GROQ_API_KEY",
        ),
        # LM Studio (ADR 0002 amendment); Ollama: http://localhost:11434/v1
        "local": ProviderSettings(base_url="http://localhost:1234/v1", model="qwen3-4b"),
    }


class LLMSettings(_Section):
    chain: list[str] = Field(default_factory=lambda: ["gemini", "groq", "local"])
    providers: dict[str, ProviderSettings] = Field(default_factory=_default_providers)
    timeout_s: float = Field(default=20.0, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _chain_names_known(self) -> Self:
        unknown = [name for name in self.chain if name not in self.providers]
        if unknown:
            raise ValueError(f"chain references undefined providers: {', '.join(unknown)}")
        return self


class SearchSettings(_Section):
    provider: Literal["ddgs", "searxng", "tavily"] = "ddgs"
    max_results: int = Field(default=5, ge=1)
    fetch_pages: int = Field(default=3, ge=0)
    searxng_url: str | None = None
    api_key_env: str = "TAVILY_API_KEY"  # used by the tavily provider only

    @model_validator(mode="after")
    def _searxng_needs_url(self) -> Self:
        if self.provider == "searxng" and not self.searxng_url:
            raise ValueError("searxng_url is required when provider = 'searxng'")
        return self


class RouterSettings(_Section):
    threshold: int = Field(default=85, ge=0, le=100)
    margin: int = Field(default=5, ge=0, le=100)
    app_aliases: dict[str, str | list[str]] = Field(default_factory=dict)
    site_aliases: dict[str, str] = Field(default_factory=dict)


class TTSSettings(_Section):
    enabled: bool = False
    provider: Literal["sapi"] = "sapi"  # the Piper Issue extends this Literal
    # Case-insensitive substrings matched against installed SAPI voice names.
    voice_ru: str = Field(default="Irina", min_length=1)
    voice_en: str = Field(default="Zira", min_length=1)
    rate: int = Field(default=0, ge=-10, le=10)
    volume: int = Field(default=100, ge=0, le=100)
    max_chars: int = Field(default=1500, ge=1)  # spoken text per reply; cut at a sentence end
    first_chunk_max_chars: int = Field(default=200, ge=1)  # size of the first spoken chunk


class Settings(_Section):
    app: AppSettings = Field(default_factory=AppSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    # Not validated yet: owned by later Issues (STT, wake word, UI); accepted and ignored.
    stt: dict[str, Any] = Field(default_factory=dict)
    wakeword: dict[str, Any] = Field(default_factory=dict)
    voice: dict[str, Any] = Field(default_factory=dict)
    ui: dict[str, Any] = Field(default_factory=dict)

    # env-variable name -> secret value; never serialized, never in repr().
    _api_keys: dict[str, SecretStr] = PrivateAttr(default_factory=dict)

    def with_api_keys(self, env: Mapping[str, str]) -> Self:
        """Pick up the keys this config refers to from ``env``; blank values count as missing."""
        names = {(p.api_key_env or "").strip() for p in self.llm.providers.values()}
        names.add(self.search.api_key_env.strip())
        names.discard("")
        self._api_keys = {n: SecretStr(env[n].strip()) for n in names if env.get(n, "").strip()}
        return self

    def api_key(self, env_name: str | None) -> SecretStr | None:
        """Secret stored under ``env_name`` (``None`` if absent or ``env_name`` is blank)."""
        env_name = (env_name or "").strip()
        return self._api_keys.get(env_name) if env_name else None

    def secret_values(self) -> list[str]:
        """Plain secret values, only for registering them with the log redaction filter."""
        return [secret.get_secret_value() for secret in self._api_keys.values()]

    def available_providers(self) -> list[str]:
        """LLM providers of the chain that can be used, in chain order.

        A provider with ``api_key_env`` but no key is unavailable (degraded start, not an error);
        providers without ``api_key_env`` (local) are always available.
        """
        return [
            name
            for name in self.llm.chain
            if not (env_name := (self.llm.providers[name].api_key_env or "").strip())
            or env_name in self._api_keys
        ]
