"""Locate and load ``luna.toml`` into :class:`Settings`.

Search order: explicit ``--config PATH`` -> ``%APPDATA%\\LUNA\\luna.toml`` -> ``./luna.toml``.
A missing file (outside an explicit path) means defaults. User values are deep-merged over the
defaults, so overriding one key of a provider keeps the rest.

API keys come from the process environment, then from ``.env`` (``./.env``, then
``%APPDATA%\\LUNA\\.env``) and are stored as ``SecretStr`` on the settings object.
Any problem with the config raises :class:`~luna.core.errors.ConfigError` naming the key.
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError

from luna.config.settings import Settings
from luna.core.errors import ConfigError

CONFIG_FILENAME = "luna.toml"
ENV_FILENAME = ".env"


def candidate_paths(
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> list[Path]:
    """Return config file locations in priority order."""
    env = os.environ if env is None else env
    paths: list[Path] = []
    if explicit is not None:
        paths.append(explicit)
    appdata = env.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "LUNA" / CONFIG_FILENAME)
    paths.append((cwd or Path.cwd()) / CONFIG_FILENAME)
    return paths


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _format_validation_error(path: Path | None, exc: ValidationError) -> str:
    where = f" in {path}" if path else ""
    lines = [
        f"  {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return f"Invalid configuration{where}:\n" + "\n".join(lines)


def _read_secrets(env: Mapping[str, str], cwd: Path) -> dict[str, str]:
    """Environment variables win over ``.env`` files; the nearest ``.env`` wins over APPDATA's."""
    files = [cwd / ENV_FILENAME]
    if appdata := env.get("APPDATA"):
        files.append(Path(appdata) / "LUNA" / ENV_FILENAME)
    merged: dict[str, str] = {}
    for file in reversed(files):
        if file.is_file():
            merged.update({k: v for k, v in dotenv_values(file).items() if v is not None})
    merged.update(env)
    return merged


def load_settings(
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Settings:
    """Load settings from the first existing config file, or return defaults.

    An explicitly given path must exist. Raises ``ConfigError`` for a missing explicit file,
    malformed TOML or invalid values.
    """
    env = os.environ if env is None else env
    cwd = cwd or Path.cwd()
    if explicit is not None and not explicit.is_file():
        raise ConfigError(f"Config file not found: {explicit}")
    data: dict[str, Any] = {}
    source: Path | None = None
    for path in candidate_paths(explicit, env, cwd):
        if path.is_file():
            source = path
            try:
                with path.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
            except OSError as exc:
                raise ConfigError(f"Cannot read {path}: {exc}") from exc
            break
    try:
        merged = _deep_merge(Settings().model_dump(), data)
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(source, exc)) from exc
    return settings.with_api_keys(_read_secrets(env, cwd))
