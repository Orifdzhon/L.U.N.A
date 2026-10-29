"""Shipped LLM presets: data validity and the URL rules (ADR 0011 D1)."""

import copy
import re

import pytest

from luna.core.errors import ConfigError
from luna.llm.presets import FREE_VALUES, load_presets, parse_presets, validate_base_url

EXPECTED = {
    "groq",
    "gemini",
    "openrouter",
    "mistral",
    "qwen",
    "lmstudio",
    "ollama",
    "cerebras",
    "deepseek",
    "together",
}


def test_all_adr_presets_load():
    presets = load_presets()
    assert {p.id for p in presets} == EXPECTED
    assert len({p.id for p in presets}) == len(presets)


def test_every_preset_is_valid():
    for p in load_presets():
        validate_base_url(p.base_url)
        assert p.free in FREE_VALUES
        if p.free != "local":
            assert p.base_url.startswith("https://")
            assert p.key_env and re.fullmatch(r"[A-Z0-9_]+", p.key_env)
            assert p.key_url and p.key_url.startswith("https://")
        else:
            assert p.key_env is None
        assert p.free_note


def test_free_presets_come_before_paid_ones():
    order = [p.free for p in load_presets()]
    assert order.index("paid") > max(i for i, f in enumerate(order) if f in ("yes", "local"))


def test_builtin_names_match_defaults():
    from luna.config.settings import Settings

    by_id = {p.id: p for p in load_presets()}
    defaults = Settings().llm.providers
    for name in ("groq", "gemini"):
        assert by_id[name].base_url == defaults[name].base_url
        assert by_id[name].key_env == defaults[name].api_key_env


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "http://localhost:1234/v1",
        "http://127.0.0.1:11434/v1",
        "http://[::1]:8080/v1",
    ],
)
def test_valid_urls(url):
    assert validate_base_url(f"  {url} ") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "ftp://example.com",
        "https://user:pw@example.com/v1",
        "https://@example.com/v1",
        "https://example.com/v1?key=1",
        "https://example.com/v1#frag",
        "https://",
        "example.com/v1",
        "http://localhost.evil.com/v1",
        "https://example.com:notaport/v1",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def _doc():
    return {
        "preset": [
            {
                "id": "x",
                "label": "X",
                "base_url": "https://x.example/v1",
                "key_env": "X_KEY",
                "key_url": "https://x.example/keys",
                "free": "yes",
                "tools": "yes",
            }
        ]
    }


def test_parse_minimal_ok():
    assert parse_presets(_doc())[0].id == "x"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", "http://x.example/v1"),
        ("key_env", "lower-case"),
        ("free", "maybe"),
        ("tools", "sometimes"),
        ("id", "Bad Id"),
        ("key_url", "http://x.example"),
        ("quirks", {"bogus": 1}),
    ],
)
def test_parse_rejects_bad_fields(field, value):
    doc = copy.deepcopy(_doc())
    doc["preset"][0][field] = value
    with pytest.raises(ConfigError, match=r"'x'|invalid id"):
        parse_presets(doc)


def test_parse_rejects_cloud_without_key_and_duplicates():
    doc = _doc()
    del doc["preset"][0]["key_env"]
    with pytest.raises(ConfigError, match="key_env"):
        parse_presets(doc)
    doc = _doc()
    doc["preset"].append(copy.deepcopy(doc["preset"][0]))
    with pytest.raises(ConfigError, match="duplicate"):
        parse_presets(doc)
    with pytest.raises(ConfigError):
        parse_presets({})
