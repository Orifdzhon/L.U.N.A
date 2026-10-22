"""Real calls to every configured provider (manual: ``uv run pytest -m live tests/integration``).

Keys come from the environment / ``.env``; a provider without its key, or a local server that is
not running, is skipped.
"""

import os

import pytest
from dotenv import load_dotenv
from pydantic import SecretStr

from luna.config.settings import LLMSettings
from luna.core.errors import ProviderUnavailable
from luna.llm.base import Message, ToolSchema
from luna.llm.openai_compat import OpenAICompatProvider, Quirks

pytestmark = pytest.mark.live

load_dotenv()

SETTINGS = LLMSettings()
TIME_TOOL = ToolSchema(
    "get_datetime", "Return the current date and time.", {"type": "object", "properties": {}}
)


def build(name: str) -> OpenAICompatProvider:
    cfg = SETTINGS.providers[name]
    key = os.environ.get(cfg.api_key_env or "", "").strip() if cfg.api_key_env else ""
    if cfg.api_key_env and not key:
        pytest.skip(f"{cfg.api_key_env} is not set")
    return OpenAICompatProvider(
        name,
        cfg.base_url,
        cfg.model,
        SecretStr(key) if key else None,
        Quirks(cfg.supports_tool_choice, cfg.reasoning_effort, cfg.max_tokens),
    )


@pytest.mark.parametrize("name", list(SETTINGS.providers))
def test_plain_reply(name: str) -> None:
    provider = build(name)
    try:
        response = provider.chat([Message("user", "Say hello in one word.")], timeout_s=60)
    except ProviderUnavailable as exc:
        if SETTINGS.providers[name].api_key_env is None:
            pytest.skip(f"local server not reachable: {exc}")
        raise
    assert response.text
    assert response.provider == name


@pytest.mark.parametrize("name", list(SETTINGS.providers))
def test_tool_call(name: str) -> None:
    provider = build(name)
    try:
        response = provider.chat(
            [Message("user", "What time is it now? Use the tool.")], [TIME_TOOL], timeout_s=60
        )
    except ProviderUnavailable as exc:
        if SETTINGS.providers[name].api_key_env is None:
            pytest.skip(f"local server not reachable: {exc}")
        raise
    assert response.tool_calls
    assert response.tool_calls[0].name == "get_datetime"
