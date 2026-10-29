"""Dark title bar through the Desktop Window Manager (Windows 10 1809+ / 11); no-op elsewhere."""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # 19 on Windows 10 before 20H1; 20 from 20H1 on


def enable_dark_title_bar(hwnd: int) -> bool:
    """Ask DWM for a dark title bar on ``hwnd``. False when unsupported or refused."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        value = ctypes.c_int(1)
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except (OSError, AttributeError):
        logger.debug("DwmSetWindowAttribute is not available", exc_info=True)
        return False
    return result == 0
