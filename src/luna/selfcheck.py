"""``luna --self-check``: prove that a (frozen) build can import its native parts.

Used as the smoke test of the Windows installer build (ADR 0010 D1): ``--version`` alone does not
import any native module, so a missing DLL or data file would only show up on the user's PC.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from collections.abc import Callable, Sequence

#: Native packages every edition ships.
CORE_MODULES: tuple[str, ...] = (
    "ctranslate2",
    "onnxruntime",
    "faster_whisper",
    "vosk",
    "sounddevice",
    "miniaudio",
    "av",
    "lxml",
    "trafilatura",
    "ddgs",
    "edge_tts",
)
#: Shipped by the personal edition only (it carries the GPL espeak-ng code).
PERSONAL_MODULES: tuple[str, ...] = ("sherpa_onnx",)


def _check_module(name: str) -> str:
    module = importlib.import_module(name)
    return f"{getattr(module, '__version__', 'ok')}"


def _check_pyside() -> str:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401

    return QtCore.qVersion()


def _check_silero_vad() -> str:
    from faster_whisper.vad import get_vad_model

    get_vad_model()
    return "Silero VAD session created"


def _check_site_aliases() -> str:
    from luna.tools.builtin.open_website import default_site_aliases

    return f"{len(default_site_aliases())} site aliases"


def _check_app_aliases() -> str:
    from luna.tools.app_matching import default_app_aliases

    return f"{len(default_app_aliases())} app aliases"


def _check_luna_modules() -> str:
    for name in ("luna.app", "luna.ui.qt.app", "luna.ui.cli"):
        importlib.import_module(name)
    return "luna modules import"


def _check_keyring() -> str:
    # The saved API keys live in Credential Manager; a frozen build without the Windows backend
    # silently falls back to the "fail" keyring and loses them.
    import keyring

    backend = type(keyring.get_keyring()).__name__
    if sys.platform == "win32" and backend != "WinVaultKeyring":
        raise RuntimeError(f"keyring backend is {backend}, expected WinVaultKeyring")
    return f"keyring backend {backend}"


def build_checks(has_sherpa: bool) -> list[tuple[str, Callable[[], str]]]:
    checks: list[tuple[str, Callable[[], str]]] = [
        (name, lambda n=name: _check_module(n)) for name in CORE_MODULES
    ]
    checks.append(("PySide6", _check_pyside))
    checks.append(("silero-vad", _check_silero_vad))
    if has_sherpa:
        checks.extend((name, lambda n=name: _check_module(n)) for name in PERSONAL_MODULES)
    checks.extend(
        [
            ("luna", _check_luna_modules),
            ("keyring", _check_keyring),
            ("site_aliases.toml", _check_site_aliases),
            ("app_aliases.toml", _check_app_aliases),
        ]
    )
    return checks


def run_self_check(
    checks: Sequence[tuple[str, Callable[[], str]]] | None = None,
    out: Callable[[str], None] | None = None,
) -> int:
    """Run every check, print one line each, return 0 if all passed, else 1."""
    if out is None:
        out = print
    if checks is None:
        checks = build_checks(importlib.util.find_spec("sherpa_onnx") is not None)
    failed = 0
    for name, check in checks:
        try:
            detail = check()
        except Exception as exc:  # any failure of a check is a failed build
            failed += 1
            out(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            out(f"ok   {name}: {detail}")
    edition = "personal" if importlib.util.find_spec("sherpa_onnx") is not None else "public"
    frozen = bool(getattr(sys, "frozen", False))
    out(f"edition={edition} frozen={frozen} failed={failed}")
    return 1 if failed else 0
