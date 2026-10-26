"""Live check: the model passes a query to open_website (manual: ``pytest -m live``)."""

import os

import pytest
from pydantic import SecretStr

from luna.config.settings import LLMSettings
from luna.core.errors import ProviderUnavailable, RateLimited
from luna.core.prompts import build_system_prompt
from luna.llm.base import Message, ToolSchema
from luna.llm.openai_compat import OpenAICompatProvider, Quirks
from luna.tools.builtin.open_website import OpenWebsiteTool

pytestmark = pytest.mark.live


class _NoBrowser:
    def open_url(self, url: str) -> None:  # never called: only the schema is used
        raise AssertionError(url)


def test_groq_passes_query_to_open_website() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    cfg = LLMSettings().providers["groq"]
    key = os.environ.get(cfg.api_key_env or "", "").strip()
    if not key:
        pytest.skip("GROQ_API_KEY is not set")
    provider = OpenAICompatProvider(
        "groq",
        cfg.base_url,
        cfg.model,
        SecretStr(key),
        Quirks(cfg.supports_tool_choice, cfg.reasoning_effort, cfg.max_tokens),
        connect_timeout_s=5,
    )
    tool = OpenWebsiteTool(_NoBrowser())
    schema = ToolSchema(tool.name, tool.description, tool.parameters)
    messages = [
        Message("system", build_system_prompt("ru", "2026-10-27 12:00")),
        Message("user", "открой ютуб и найди lofi hip hop radio"),
    ]
    try:
        response = provider.chat(messages, [schema], timeout_s=60)
    except (RateLimited, ProviderUnavailable) as exc:
        pytest.skip(f"groq unavailable: {exc}")
    assert response.tool_calls
    call = response.tool_calls[0]
    assert call.name == "open_website"
    assert "lofi" in str(call.arguments.get("query", "")).lower()
