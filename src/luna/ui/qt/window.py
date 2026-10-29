"""The L.U.N.A. window: orb, chat bubbles, input row, status line. Close hides to tray."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from luna.platform.windows.dwm import enable_dark_title_bar
from luna.ui.qt.chat import ChatView
from luna.ui.qt.orb import OrbWidget
from luna.ui.qt.theme import PALETTE, mic_icon, send_icon, stylesheet
from luna.ui.shell.state import ConversationLine, IconState
from luna.ui.shell.strings import t


class _InputLine(QLineEdit):
    def __init__(self, on_cancel: Callable[[], None]) -> None:
        super().__init__()
        self._on_cancel = on_cancel

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel()
            event.accept()
            return
        super().keyPressEvent(event)


def _round_button(name: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName(name)
    button.setToolTip(tooltip)
    button.setFixedSize(36, 36)
    button.setIconSize(QSize(36, 36))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class QtWindowView(QWidget):
    """Implements ``WindowView``. UI thread only; needs a ``QApplication``."""

    def __init__(
        self,
        *,
        on_submit: Callable[[str], None],
        on_cancel: Callable[[], None],
        on_closed: Callable[[], None],
        on_listen: Callable[[], None] | None = None,
        on_open_file: Callable[[int, str], None] | None = None,
        history_lines: int = 200,
        lang: str = "en",
    ) -> None:
        super().__init__()
        self._on_submit = on_submit
        self._on_closed = on_closed
        self._on_listen = on_listen
        self._lang = lang
        self._voice_reason = ""
        self._voice_available = True
        self._recording = False
        self.setObjectName("luna-window")
        self.setWindowTitle(t("window.title", lang))
        self.resize(420, 640)
        self.setMinimumSize(360, 480)
        self.setStyleSheet(stylesheet())
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.orb = OrbWidget()
        self.caption = QLabel("")
        self.caption.setObjectName("caption")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat = ChatView(history_lines, on_open_file, lang)
        self.input = _InputLine(on_cancel)
        self.input.setPlaceholderText(t("window.input_placeholder", lang))
        self.input.returnPressed.connect(self._submit)
        self.input.textChanged.connect(self._sync_send)
        self.send_button = _round_button("send", t("window.send", lang))
        self.send_button.setIcon(send_icon(PALETTE["bg"]))
        self.send_button.clicked.connect(self._submit)
        self.mic_button = _round_button("mic", t("window.mic", lang))
        self.mic_button.setIcon(mic_icon(PALETTE["text"]))
        self.mic_button.setCheckable(True)
        self.mic_button.clicked.connect(self._mic_clicked)
        self.status = QLabel("")
        self.status.setObjectName("status")

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        row.addWidget(self.mic_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        layout.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.caption)
        layout.addWidget(self.chat, 1)
        layout.addLayout(row)
        layout.addWidget(self.status)
        self._sync_send()

    # ---- input -----------------------------------------------------------------------

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text or not self.input.isEnabled():
            return
        self.input.clear()
        self._on_submit(text)

    def _sync_send(self) -> None:
        self.send_button.setEnabled(self.input.isEnabled() and bool(self.input.text().strip()))

    def _mic_clicked(self) -> None:
        self.mic_button.setChecked(self._recording)  # the state comes from the controller
        if self._on_listen is not None:
            self._on_listen()

    # ---- WindowView ------------------------------------------------------------------

    def append(self, line: ConversationLine) -> None:
        self.chat.add_line(line)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_input_busy(self, busy: bool) -> None:
        self.input.setEnabled(not busy)
        self._sync_send()
        if not busy and self.isVisible():
            self.input.setFocus()

    def set_state(self, state: IconState, caption: str) -> None:
        self.orb.set_state(state)
        self.caption.setText(caption)
        self._recording = state is IconState.RECORDING
        self.mic_button.setChecked(self._recording)
        self.mic_button.setProperty("recording", "true" if self._recording else "false")
        self.mic_button.style().unpolish(self.mic_button)
        self.mic_button.style().polish(self.mic_button)

    def set_voice_available(self, available: bool, reason: str) -> None:
        self._voice_available = available
        self._voice_reason = reason
        self.mic_button.setEnabled(available)
        self.mic_button.setToolTip(t("window.mic", self._lang) if available else reason)

    def show(self) -> None:
        super().show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    @property
    def visible(self) -> bool:
        return self.isVisible()

    # ---- Qt events -------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        enable_dark_title_bar(int(self.winId()))

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
        self._on_closed()
