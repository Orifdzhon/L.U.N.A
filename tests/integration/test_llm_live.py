"""Real calls to every configured provider (manual: ``uv run pytest -m live tests/integration``).

Keys come from the environment / ``.env`` (loaded only when a live test actually runs). A
provider without its key, a rate-limited free tier or a local server that is not running is
skipped, not failed.
"""

import os
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from luna.config.settings import LLMSettings
from luna.core.errors import ProviderUnavailable, RateLimited
from luna.llm.base import ChatResponse, Message, ToolSchema
from luna.llm.openai_compat import OpenAICompatProvider, Quirks

pytestmark = pytest.mark.live

NAMES = list(LLMSettings().providers)  # static defaults only: no I/O at import time
TIME_TOOL = ToolSchema(
    "get_datetime", "Return the current date and time.", {"type": "object", "properties": {}}
)


@pytest.fixture(scope="module")
def settings() -> Iterator[LLMSettings]:
    from dotenv import load_dotenv

    load_dotenv()  # runs only for live tests (the module is otherwise deselected)
    yield LLMSettings()


def build(settings: LLMSettings, name: str) -> OpenAICompatProvider:
    cfg = settings.providers[name]
    key = os.environ.get(cfg.api_key_env or "", "").strip() if cfg.api_key_env else ""
    if cfg.api_key_env and not key:
        pytest.skip(f"{cfg.api_key_env} is not set")
    return OpenAICompatProvider(
        name,
        cfg.base_url,
        cfg.model,
        SecretStr(key) if key else None,
        Quirks(cfg.supports_tool_choice, cfg.reasoning_effort, cfg.max_tokens),
        connect_timeout_s=settings.connect_timeout_s,
    )


def call(
    settings: LLMSettings, name: str, messages: list[Message], tools: list[ToolSchema]
) -> ChatResponse:
    provider = build(settings, name)
    try:
        return provider.chat(messages, tools, timeout_s=max(settings.timeout_s, 60))
    except RateLimited as exc:
        pytest.skip(f"rate limited: {exc}")
    except ProviderUnavailable as exc:
        if settings.providers[name].api_key_env is None:
            pytest.skip(f"local server not reachable: {exc}")
        raise


@pytest.mark.parametrize("name", NAMES)
def test_plain_reply(settings: LLMSettings, name: str) -> None:
    response = call(settings, name, [Message("user", "Say hello in one word.")], [])
    assert response.text
    assert response.provider == name


@pytest.mark.parametrize("name", NAMES)
def test_tool_call_round_trip(settings: LLMSettings, name: str) -> None:
    history = [Message("user", "What time is it now? Use the tool.")]
    first = call(settings, name, history, [TIME_TOOL])
    assert first.tool_calls
    tool_call = first.tool_calls[0]
    assert tool_call.name == "get_datetime"
    # Second leg: send the assistant tool call and its result back (Gemini 3 may need more here).
    history += [
        Message("assistant", first.text, tool_calls=first.tool_calls),
        Message("tool", "2026-10-23 12:00 (Friday)", tool_call_id=tool_call.id),
    ]
    second = call(settings, name, history, [TIME_TOOL])
    assert second.text or second.tool_calls
