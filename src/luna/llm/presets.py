"""LLM provider presets shipped as data (ADR 0011, Feature D)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from luna.core.errors import ConfigError

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
KEY_ENV_PATTERN = re.compile(r"^[A-Z0-9_]+$")
PRESET_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
FREE_VALUES = ("yes", "trial", "paid", "local")
TOOLS_VALUES = ("yes", "per_model", "unknown")
QUIRK_KEYS = ("supports_tool_choice", "reasoning_effort", "max_tokens")


def validate_base_url(url: str) -> str:
    """Return ``url`` stripped, or raise ``ValueError``.

    ``https://`` is required, except ``http://`` for local hosts; no user-info, query or fragment.
    """
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise ValueError("base_url is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("base_url must start with https://")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise ValueError("base_url must use https:// (http:// is only allowed for local hosts)")
    if "@" in parts.netloc:
        raise ValueError("base_url must not contain a user name or password")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not contain a query or a fragment")
    return url


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    base_url: str
    default_model: str | None
    key_env: str | None
    key_url: str | None
    free: Literal["yes", "trial", "paid", "local"]
    free_note: str
    tools: Literal["yes", "per_model", "unknown"]
    quirks: Mapping[str, Any]


def _text(raw: Mapping[str, Any], key: str, where: str, *, required: bool = False) -> str | None:
    value = raw.get(key)
    if value is None or value == "":
        if required:
            raise ConfigError(f"LLM preset {where}: '{key}' is required")
        return None
    if not isinstance(value, str):
        raise ConfigError(f"LLM preset {where}: '{key}' must be a string")
    return value


def _parse(raw: Mapping[str, Any]) -> ProviderPreset:
    preset_id = _text(raw, "id", "?", required=True) or ""
    where = repr(preset_id)
    if not PRESET_ID_PATTERN.match(preset_id):
        raise ConfigError(f"LLM preset {where}: invalid id")
    label = _text(raw, "label", where, required=True) or ""
    base_url = _text(raw, "base_url", where, required=True) or ""
    try:
        base_url = validate_base_url(base_url)
    except ValueError as exc:
        raise ConfigError(f"LLM preset {where}: {exc}") from None
    key_env = _text(raw, "key_env", where)
    if key_env is not None and not KEY_ENV_PATTERN.match(key_env):
        raise ConfigError(f"LLM preset {where}: key_env must match [A-Z0-9_]+")
    key_url = _text(raw, "key_url", where)
    if key_url is not None and not key_url.startswith("https://"):
        raise ConfigError(f"LLM preset {where}: key_url must be https://")
    free = raw.get("free")
    if free not in FREE_VALUES:
        raise ConfigError(f"LLM preset {where}: free must be one of {', '.join(FREE_VALUES)}")
    tools = raw.get("tools")
    if tools not in TOOLS_VALUES:
        raise ConfigError(f"LLM preset {where}: tools must be one of {', '.join(TOOLS_VALUES)}")
    if free != "local" and key_env is None:
        raise ConfigError(f"LLM preset {where}: a cloud preset needs key_env")
    quirks = raw.get("quirks", {})
    if not isinstance(quirks, dict) or set(quirks) - set(QUIRK_KEYS):
        raise ConfigError(f"LLM preset {where}: quirks may only set {', '.join(QUIRK_KEYS)}")
    return ProviderPreset(
        id=preset_id,
        label=label,
        base_url=base_url,
        default_model=_text(raw, "default_model", where),
        key_env=key_env,
        key_url=key_url,
        free=cast(Any, free),
        free_note=_text(raw, "free_note", where) or "",
        tools=cast(Any, tools),
        quirks=dict(quirks),
    )


def parse_presets(data: Mapping[str, Any]) -> tuple[ProviderPreset, ...]:
    """Validate the decoded TOML document; ``ConfigError`` names the offending preset."""
    items = data.get("preset")
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        raise ConfigError("llm_presets.toml: expected a list of [[preset]] tables")
    presets = tuple(_parse(item) for item in items)
    ids = [p.id for p in presets]
    if len(set(ids)) != len(ids):
        raise ConfigError("llm_presets.toml: duplicate preset ids")
    return presets


@cache
def load_presets() -> tuple[ProviderPreset, ...]:
    """Presets of ``luna/data/llm_presets.toml``, in display order (free ones first)."""
    raw = (resources.files("luna") / "data" / "llm_presets.toml").read_text(encoding="utf-8")
    return parse_presets(tomllib.loads(raw))
