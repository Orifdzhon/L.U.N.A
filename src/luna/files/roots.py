"""Which folders the file search may look into (ADR 0011, decision 1). OS-neutral logic.

The OS parts (known folders, fixed drives) are injected, see ``luna.platform.factory``.
UNC paths are never searched.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from luna.config.settings import FilesSettings
from luna.files.base import RootDir

ONEDRIVE_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def is_unc(path: str | Path) -> bool:
    text = str(path).replace("/", "\\")
    return text.startswith("\\\\")


def resolve_roots(
    conf: FilesSettings,
    *,
    known_folders: Callable[[], Sequence[tuple[str, Path]]],
    drives: Callable[[], Sequence[Path]],
    environ: Mapping[str, str] | None = None,
) -> list[RootDir]:
    """User folders (+ OneDrive, extra roots) first, then fixed drives; existing, de-duplicated."""
    env = os.environ if environ is None else environ
    candidates: list[RootDir] = []
    if "user" in conf.roots:
        candidates += [RootDir(path, label, True) for label, path in known_folders()]
        for var in ONEDRIVE_VARS:
            value = env.get(var, "").strip()
            if value:
                candidates.append(RootDir(Path(value), "OneDrive", True))
    candidates += [RootDir(Path(p), p.name or str(p), True) for p in conf.extra_roots]
    if "drives" in conf.roots:
        candidates += [RootDir(p, str(p).rstrip("\\/"), False) for p in drives()]
    roots: list[RootDir] = []
    seen: set[str] = set()
    for root in candidates:
        key = os.path.normcase(str(root.path)).rstrip("\\/")
        if key in seen or is_unc(root.path) or not root.path.is_absolute():
            continue
        try:
            if not root.path.is_dir():
                continue
        except OSError:
            continue
        seen.add(key)
        roots.append(root)
    return roots
