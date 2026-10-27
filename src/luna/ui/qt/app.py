"""Tray mode runner: QApplication, views, ShellController (ADR 0009, ARCHITECTURE 4.4).

The only module that wires Qt to the shell. PySide6 is imported here (and in its siblings)
only; ``luna --text`` and the core never import it.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from luna.app import App, build_app
from luna.config import secrets as credentials
from luna.config.secrets import CredentialStore
from luna.config.settings import Settings
from luna.platform.base import Autostart, SingleInstance
from luna.platform.factory import create_autostart
from luna.ui.qt.dispatch import QtUiThread
from luna.ui.qt.keys_dialog import KeysDialog
from luna.ui.qt.tray import QtTrayView
from luna.ui.qt.window import QtWindowView
from luna.ui.shell.controller import ShellController, arm_watchdog
from luna.ui.shell.keys import KeysPresenter, has_cloud_llm_key
from luna.ui.shell.launch import own_launch_command
from luna.ui.shell.state import ConversationLine

logger = logging.getLogger(__name__)

ALREADY_RUNNING = "L.U.N.A. is already running (see the tray icon)"
NO_TRAY_LINE = "No system tray is available: this window stays open. Close it to quit."
SIGNAL_POLL_MS = 200


def _application() -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    return QApplication(sys.argv[:1])


def show_already_running() -> None:
    """Tray mode, second launch: tell the user in a message box (no console to print to)."""
    _application()
    QMessageBox.information(None, "L.U.N.A.", ALREADY_RUNNING)


def run_tray(
    settings: Settings,
    args: argparse.Namespace,
    *,
    instance: SingleInstance,
    autostart: Autostart | None = None,
    watchdog: Callable[[], None] = arm_watchdog,
    app_factory: Callable[..., App] = build_app,
    on_ready: Callable[[QApplication, ShellController], Any] | None = None,
    keys_store: CredentialStore | None = None,
) -> int:
    """Run the tray shell until Quit. ``instance`` is released when the shell quits."""
    qt_app = _application()
    qt_app.setQuitOnLastWindowClosed(False)
    app = app_factory(settings, start_wake=False)
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    ui = QtUiThread()
    holder: dict[str, ShellController] = {}

    def quit_shell() -> None:
        holder["controller"].quit()

    def on_closed() -> None:
        if tray_available:
            holder["controller"].window_closed()
        else:
            quit_shell()  # without a tray icon the window is the only handle

    def on_quit() -> None:
        try:
            instance.release()
        finally:
            qt_app.quit()

    window = QtWindowView(
        on_submit=lambda text: holder["controller"].submit(text),
        on_cancel=lambda: holder["controller"].cancel(),
        on_closed=on_closed,
    )
    keys_dialogs: list[KeysDialog] = []

    def show_keys() -> None:
        if not keys_dialogs:
            store = keys_store if keys_store is not None else credentials.default_store()
            keys_dialogs.append(KeysDialog(KeysPresenter(settings, store)))
        dialog = keys_dialogs[0]
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    tray = QtTrayView(
        on_open=lambda: holder["controller"].toggle_window(),
        on_set_wake=lambda on: holder["controller"].set_wake(on),
        on_set_voice=lambda on: holder["controller"].set_voice(on),
        on_set_autostart=lambda on: holder["controller"].set_autostart(on),
        on_quit=quit_shell,
        on_keys=show_keys,
    )
    controller = ShellController(
        app,
        tray,
        window,
        ui,
        autostart=autostart if autostart is not None else create_autostart(),
        launch=lambda: own_launch_command(Path(sys.executable), args.config, Path.cwd()),
        conf=settings.ui,
        watchdog=watchdog,
        on_quit=on_quit,
    )
    holder["controller"] = controller
    qt_app.aboutToQuit.connect(controller.quit)

    def on_sigint(_signum: int, _frame: FrameType | None) -> None:
        controller.quit()

    previous = signal.signal(signal.SIGINT, on_sigint)
    ticker = QTimer()  # lets the interpreter run the SIGINT handler while Qt is idle
    ticker.timeout.connect(lambda: None)
    ticker.start(SIGNAL_POLL_MS)

    voice = app.voice_input
    wake = settings.wakeword.enabled and voice is not None and voice.enabled
    minimized = bool(getattr(args, "minimized", False) or settings.ui.start_minimized)
    controller.start(wake=wake, minimized=minimized and tray_available)
    if not tray_available:
        window.append(ConversationLine("system", NO_TRAY_LINE))
        window.show()
    if not has_cloud_llm_key(settings):
        QTimer.singleShot(0, show_keys)  # a nudge on every start until a key is saved
    if on_ready is not None:
        on_ready(qt_app, controller)
    try:
        qt_app.exec()
    finally:
        ticker.stop()
        for dialog in keys_dialogs:
            dialog.close()
            shiboken6.delete(dialog)  # free the widgets before the QApplication goes away
        keys_dialogs.clear()
        signal.signal(signal.SIGINT, previous)
        controller.quit()  # idempotent: covers an exit that did not come through Quit
    return 0
