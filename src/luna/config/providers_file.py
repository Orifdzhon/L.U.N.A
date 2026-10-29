"""The generated ``%APPDATA%\\LUNA\\providers.toml`` written by the "Models / providers" dialog.

It holds only ``[llm] chain`` and the ``[llm.providers.<name>]`` tables, and is merged over
``luna.toml`` by the loader (ADR 0011 D11), so the owner's hand-written file is never rewritten.
API keys are never part of it (``ProviderSettings`` has no key field, only the variable name).

Clearing a value: TOML has no null, so the overlay writes a sentinel where ``luna.toml`` sets a
value the dialog wants unset: ``"auto"`` for ``supports_tools`` / ``supports_tool_choice`` /
``max_tokens`` and ``""`` for ``api_key_env`` / ``reasoning_effort``. :func:`resolve_overlay`
turns them back into ``None`` before validation.
"""

from __future__ import annotations

import contextlib
import os
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from luna.config.settings import ProviderSettings, Settings
from luna.core.errors import ConfigError
from luna.llm.presets import validate_base_url

PROVIDERS_FILENAME = "providers.toml"
HEADER = (
    "# Managed by L.U.N.A. - edit in Models / providers (tray menu). Manual edits are lost.\n\n"
)
_ALLOWED_LLM_KEYS = {"chain", "providers"}
CLEAR_SENTINELS: dict[str, str] = {
    "supports_tools": "auto",
    "supports_tool_choice": "auto",
    "max_tokens": "auto",
    "api_key_env": "",
    "reasoning_effort": "",
}


def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def providers_path(env: Mapping[str, str] | None = None) -> Path | None:
    """``%APPDATA%\\LUNA\\providers.toml``; ``None`` when ``APPDATA`` is not set."""
    env = os.environ if env is None else env
    appdata = env.get("APPDATA")
    return Path(appdata) / "LUNA" / PROVIDERS_FILENAME if appdata else None


def read_overlay(path: Path | None) -> dict[str, Any]:
    """The decoded overlay, ``{}`` if the file is absent. ``ConfigError`` names the file."""
    if path is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    llm = data.get("llm", {})
    if set(data) - {"llm"} or not isinstance(llm, dict) or set(llm) - _ALLOWED_LLM_KEYS:
        raise ConfigError(f"Invalid {path}: only [llm] chain and [llm.providers.*] are allowed")
    providers = llm.get("providers", {})
    if not isinstance(providers, dict):
        raise ConfigError(f"Invalid {path}: [llm.providers] must be a table")
    for name, conf in providers.items():
        url = conf.get("base_url") if isinstance(conf, dict) else None
        if url is None:
            continue  # a partial table that only tweaks a provider defined elsewhere
        try:
            validate_base_url(url if isinstance(url, str) else "")
        except ValueError as exc:
            raise ConfigError(f"Invalid {path}: provider {name!r}: {exc}") from None
    return data


def resolve_overlay(data: Mapping[str, Any]) -> dict[str, Any]:
    """Replace the clearing sentinels of ``CLEAR_SENTINELS`` with ``None`` (a copy)."""
    out = {k: v for k, v in data.items() if k != "llm"}
    llm = dict(data.get("llm", {}))
    if "providers" in llm:
        llm["providers"] = {
            name: {
                key: (None if CLEAR_SENTINELS.get(key, object()) == value else value)
                for key, value in conf.items()
            }
            for name, conf in llm["providers"].items()
        }
    out["llm"] = llm
    return out


def _provider_overlay(name: str, conf: ProviderSettings, base: Settings) -> dict[str, Any]:
    table = conf.model_dump(exclude_none=True)
    known = base.llm.providers.get(name)
    if known is not None:  # clear what luna.toml / the defaults set but the dialog unset
        for key, sentinel in CLEAR_SENTINELS.items():
            if getattr(conf, key) is None and getattr(known, key) is not None:
                table[key] = sentinel
    return table


def write_overlay(
    path: Path,
    chain: Sequence[str],
    providers: Mapping[str, ProviderSettings],
    *,
    base: Settings,
) -> None:
    """Validate ``base`` <- the new overlay, then write ``path`` atomically.

    ``ConfigError`` (nothing written) when the result is not a valid configuration.
    """
    overlay: dict[str, Any] = {
        "llm": {
            "chain": list(chain),
            "providers": {
                name: _provider_overlay(name, conf, base) for name, conf in providers.items()
            },
        }
    }
    for name, conf in providers.items():
        try:
            validate_base_url(conf.base_url)
        except ValueError as exc:
            raise ConfigError(f"Provider {name!r}: {exc}") from None
    try:
        Settings.model_validate(deep_merge(base.model_dump(), resolve_overlay(overlay)))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"Invalid providers: {problems}") from None
    text = HEADER + tomli_w.dumps(overlay)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise ConfigError(f"Cannot write {path}: {exc}") from exc
