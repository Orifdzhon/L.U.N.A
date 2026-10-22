"""Locate and load ``luna.toml`` into :class:`Settings`.

Search order: explicit ``--config PATH`` -> ``%APPDATA%\\LUNA\\luna.toml`` -> ``./luna.toml``.
A missing file (outside an explicit path) means defaults.
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

from luna.config.settings import Settings

CONFIG_FILENAME = "luna.toml"


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


def load_settings(
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Settings:
    """Load settings from the first existing config file, or return defaults.

    An explicitly given path must exist; otherwise ``FileNotFoundError`` is raised.
    """
    if explicit is not None and not explicit.is_file():
        raise FileNotFoundError(f"Config file not found: {explicit}")
    for path in candidate_paths(explicit, env, cwd):
        if path.is_file():
            with path.open("rb") as fh:
                return Settings.model_validate(tomllib.load(fh))
    return Settings()
