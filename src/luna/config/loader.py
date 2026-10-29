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

import contextlib
import logging
import os
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError

from luna.config import secrets as credentials
from luna.config.providers_file import deep_merge as _deep_merge
from luna.config.providers_file import providers_path, read_overlay, resolve_overlay
from luna.config.secrets import CredentialStore
from luna.config.settings import Settings
from luna.core.errors import ConfigError

logger = logging.getLogger(__name__)

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


def luna_toml_sets_chain(
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> bool:
    """True when the active ``luna.toml`` sets ``[llm] chain`` (the dialog's order overrides it)."""
    env = os.environ if env is None else env
    for path in candidate_paths(explicit, env, cwd):
        if path.is_file():
            try:
                with path.open("rb") as fh:
                    llm = tomllib.load(fh).get("llm", {})
            except (tomllib.TOMLDecodeError, OSError):
                return False
            return isinstance(llm, dict) and "chain" in llm
    return False


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


def _quarantine(path: Path) -> None:
    """Rename a broken overlay to ``providers.toml.bad-<timestamp>`` (best effort)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with contextlib.suppress(OSError):
        os.replace(path, path.with_name(f"{path.name}.bad-{stamp}"))


def _apply_overlay(settings: Settings, merged: dict[str, Any], path: Path | None) -> Settings:
    """Merge the dialog's ``providers.toml``; a broken one is skipped so the app can start."""
    try:
        overlay = read_overlay(path)
        if not overlay:
            return settings
        try:
            return Settings.model_validate(_deep_merge(merged, resolve_overlay(overlay)))
        except ValidationError as exc:
            raise ConfigError(_format_validation_error(path, exc)) from exc
    except ConfigError as exc:
        first = str(exc).splitlines()[0]
        logger.warning("Ignoring %s (%s); fix it in Models / providers", path, first)
        if path is not None:
            _quarantine(path)
        return settings


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
    settings = _apply_overlay(settings, merged, providers_path(env))
    secrets = _read_secrets(env, cwd)
    if store is None and real_env:
        store = credentials.default_store()
    if store is not None:  # lowest priority: only names nothing else provided
        for name in settings.api_key_names():
            if not secrets.get(name, "").strip() and (saved := store.get(name)):
                secrets[name] = saved
    return settings.with_api_keys(secrets)
