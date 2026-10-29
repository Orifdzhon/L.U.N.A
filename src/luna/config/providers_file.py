"""The generated ``%APPDATA%\\LUNA\\providers.toml`` written by the "Models / providers" dialog.

It holds only ``[llm] chain`` and the ``[llm.providers.<name>]`` tables, and is merged over
``luna.toml`` by the loader (ADR 0011 D11), so the owner's hand-written file is never rewritten.
API keys are never part of it (``ProviderSettings`` has no key field, only the variable name).
"""

from __future__ import annotations

import contextlib
import os
import tomllib
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
    return data


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
                name: conf.model_dump(exclude_none=True) for name, conf in providers.items()
            },
        }
    }
    for name, conf in providers.items():
        try:
            validate_base_url(conf.base_url)
        except ValueError as exc:
            raise ConfigError(f"Provider {name!r}: {exc}") from None
    try:
        Settings.model_validate(deep_merge(base.model_dump(), overlay))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"Invalid providers: {problems}") from None
    text = HEADER + tomli_w.dumps(overlay)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise ConfigError(f"Cannot write {path}: {exc}") from exc
