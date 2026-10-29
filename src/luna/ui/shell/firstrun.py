"""First-run marker: ``first_run_done`` in ``%LOCALAPPDATA%/LUNA/state.json``. Pure: no Qt.

The guide is shown until the user presses "Got it"; then the flag is stored and it stays quiet.
Every failure (unwritable folder, broken JSON) degrades to "not done" / "not saved": the guide
may appear again, nothing else breaks.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

KEY = "first_run_done"


def default_state_path(env: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%/LUNA/state.json`` (falls back to ``~/AppData/Local`` if unset)."""
    base = (env if env is not None else os.environ).get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "LUNA" / "state.json"


def _read(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_first_run_done(path: Path) -> bool:
    return _read(path).get(KEY) is True


def mark_first_run_done(path: Path) -> None:
    data = _read(path)
    data[KEY] = True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("[ui] could not save the first-run marker: %s", type(exc).__name__)
