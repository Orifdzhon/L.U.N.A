"""System tray view: ``QSystemTrayIcon`` plus a seven-item menu driven by ``TrayMenuState``."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from luna.ui.qt.icons import make_icons
from luna.ui.shell.state import IconState, TrayMenuState
from luna.ui.shell.strings import t


class QtTrayView:
    """Implements ``TrayView``. UI thread only; needs a ``QApplication``."""

    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_set_wake: Callable[[bool], None],
        on_set_voice: Callable[[bool], None],
        on_set_autostart: Callable[[bool], None],
        on_quit: Callable[[], None],
        on_keys: Callable[[], None] | None = None,
        on_guide: Callable[[], None] | None = None,
        lang: str = "en",
    ) -> None:
        self._on_open = on_open
        self._icons = make_icons()
        self._tray = QSystemTrayIcon(self._icons[IconState.IDLE])
        self._menu = QMenu()
        self.wake_action = self._checkable(t("tray.wake", lang), on_set_wake)
        self.voice_action = self._checkable(t("tray.voice", lang), on_set_voice)
        self.open_action = QAction(t("tray.open", lang), self._menu)
        self.open_action.triggered.connect(lambda _checked=False: on_open())
        self.autostart_action = self._checkable(t("tray.autostart", lang), on_set_autostart)
        self.keys_action = QAction(t("tray.keys", lang), self._menu)
        self.keys_action.triggered.connect(lambda _checked=False: on_keys() if on_keys else None)
        self.guide_action = QAction(t("tray.guide", lang), self._menu)
        self.guide_action.triggered.connect(lambda _checked=False: on_guide() if on_guide else None)
        self.quit_action = QAction(t("tray.quit", lang), self._menu)
        self.quit_action.triggered.connect(lambda _checked=False: on_quit())
        for action in (
            self.wake_action,
            self.voice_action,
            self.open_action,
            self.autostart_action,
            self.keys_action,
            self.guide_action,
        ):
            self._menu.addAction(action)
        self._menu.addSeparator()
        self._menu.addAction(self.quit_action)
        self._menu.setDefaultAction(self.open_action)
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

    def _checkable(self, label: str, callback: Callable[[bool], None]) -> QAction:
        action = QAction(label, self._menu)
        action.setCheckable(True)
        # ``triggered`` fires on user clicks only, not on programmatic setChecked.
        action.triggered.connect(lambda checked=False: callback(bool(checked)))
        return action

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # left click
            self._on_open()

    def set_state(self, state: IconState, tooltip: str) -> None:
        self._tray.setIcon(self._icons[state])
        self._tray.setToolTip(tooltip)

    def set_menu(self, menu: TrayMenuState) -> None:
        self.wake_action.setText(menu.wake_label)
        self.wake_action.setChecked(menu.wake_checked)
        self.wake_action.setEnabled(menu.wake_enabled)
        self.voice_action.setChecked(menu.voice_checked)
        self.voice_action.setEnabled(menu.voice_enabled)
        self.autostart_action.setChecked(menu.autostart_checked)
        self.autostart_action.setEnabled(menu.autostart_enabled)

    def notify(self, title: str, text: str) -> None:
        self._tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information)

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()
