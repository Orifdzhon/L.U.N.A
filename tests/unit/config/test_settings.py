import pytest
from pydantic import ValidationError

from luna.config.settings import ProviderSettings, SearchSettings, Settings


def test_default_chain_and_local_provider():
    s = Settings()
    assert s.llm.chain == ["groq", "gemini", "local"]
    local = s.llm.providers["local"]
    assert local.base_url == "http://localhost:1234/v1"
    assert local.api_key_env is None
    assert s.search.provider == "ddgs"
    assert (s.search.max_results, s.search.fetch_pages) == (5, 3)
    assert (s.router.threshold, s.router.margin) == (85, 5)
    assert (s.app.history_turns, s.app.request_timeout_s) == (6, 20.0)


def test_without_keys_only_local_available():
    assert Settings().available_providers() == ["local"]


def test_available_providers_follow_chain_order():
    s = Settings().with_api_keys({"GROQ_API_KEY": "k", "GEMINI_API_KEY": "k2"})
    assert s.available_providers() == ["groq", "gemini", "local"]
    s = Settings().with_api_keys({"GROQ_API_KEY": "k"})
    assert s.available_providers() == ["groq", "local"]


def test_provider_without_key_env_stays_available():
    s = Settings()
    s.llm.providers["ollama"] = ProviderSettings(
        base_url="http://localhost:11434/v1", model="qwen3:4b"
    )
    s.llm.chain.append("ollama")
    assert s.available_providers() == ["local", "ollama"]


@pytest.mark.parametrize(
    "kwargs",
    [{"max_results": 0}, {"fetch_pages": -1}, {"provider": "bing"}, {"provider": "searxng"}],
)
def test_invalid_search_values(kwargs):
    with pytest.raises(ValidationError):
        SearchSettings(**kwargs)


def test_searxng_with_url_ok():
    assert SearchSettings(provider="searxng", searxng_url="http://x").searxng_url == "http://x"


@pytest.mark.parametrize("field", ["threshold", "margin"])
@pytest.mark.parametrize("value", [-1, 101])
def test_router_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings.model_validate({"router": {field: value}})


def test_api_keys_cannot_come_from_config_data():
    with pytest.raises(ValidationError):
        Settings.model_validate({"_api_keys": {"GEMINI_API_KEY": "x"}})
