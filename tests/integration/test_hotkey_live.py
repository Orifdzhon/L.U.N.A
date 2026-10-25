"""Registers a real, unusual global hotkey (manual: ``uv run pytest -m desktop``)."""

import sys

import pytest

from luna.core.errors import ToolError

pytestmark = [
    pytest.mark.desktop,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
]

COMBO = "ctrl+alt+shift+f12"


def test_register_and_conflict():
    from luna.platform.windows.hotkey import WindowsHotkeyListener

    first = WindowsHotkeyListener()
    second = WindowsHotkeyListener()
    first.start(COMBO, lambda: None)
    try:
        with pytest.raises(ToolError, match="already taken"):
            second.start(COMBO, lambda: None)
    finally:
        first.stop()
        second.stop()
    # released again: can be registered once more
    third = WindowsHotkeyListener()
    third.start(COMBO, lambda: None)
    third.stop()
