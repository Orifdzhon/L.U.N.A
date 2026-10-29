"""Locate and load ``luna.toml`` into :class:`Settings`.

Search order: explicit ``--config PATH`` -> ``%APPDATA%\\LUNA\\luna.toml`` -> ``./luna.toml``.
A missing file (outside an explicit path) means defaults. User values are deep-merged over the
defaults, so overriding one key of a provider keeps the rest. The dialog-generated
``%APPDATA%\\LUNA\\providers.toml`` (``[llm]`` only) is merged on top of that.

API keys come from the process environment, then from ``.env`` (``./.env``, then
``%APPDATA%\\LUNA\\.env``), then from the Windows Credential Manager (``luna.config.secrets``) and
are stored as ``SecretStr`` on the settings object. The store is used only with the real process
environment or when passed explicitly.
Any problem with the config raises :class:`~luna.core.errors.ConfigError` naming the key.
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError

from luna.config import secrets as credentials
from luna.config.providers_file import deep_merge as _deep_merge
from luna.config.providers_file import providers_path, read_overlay
from luna.config.secrets import CredentialStore
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
            try:
                values = dotenv_values(file, encoding="utf-8", interpolate=False)
            except (UnicodeDecodeError, OSError) as exc:
                raise ConfigError(
                    f"Cannot read {file} ({type(exc).__name__}); save it as UTF-8 text"
                ) from None
            merged.update({k: v for k, v in values.items() if v})
    merged.update({k: v for k, v in env.items() if v})
    return merged


def load_settings(
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    store: CredentialStore | None = None,
) -> Settings:
    """Load settings from the first existing config file, or return defaults.

    An explicitly given path must exist. Raises ``ConfigError`` for a missing explicit file,
    malformed TOML or invalid values.
    """
    real_env = env is None
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
    overlay_path = providers_path(env)
    overlay = read_overlay(overlay_path)
    if overlay:  # the dialog's generated file wins over luna.toml (ADR 0011 D11)
        try:
            settings = Settings.model_validate(_deep_merge(merged, overlay))
        except ValidationError as exc:
            raise ConfigError(_format_validation_error(overlay_path, exc)) from exc
    secrets = _read_secrets(env, cwd)
    if store is None and real_env:
        store = credentials.default_store()
    if store is not None:  # lowest priority: only names nothing else provided
        for name in settings.api_key_names():
            if not secrets.get(name, "").strip() and (saved := store.get(name)):
                secrets[name] = saved
    return settings.with_api_keys(secrets)
