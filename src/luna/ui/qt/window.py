"""Small conversation window: read-only log, input line, status line. Close hides to tray."""

from __future__ import annotations

import html
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

from luna.ui.shell.state import ConversationLine

_PREFIX = {"user": "You: ", "assistant": "L.U.N.A.: ", "action": "> ", "system": "* "}


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


class QtWindowView(QWidget):
    """Implements ``WindowView``. UI thread only; needs a ``QApplication``."""

    def __init__(
        self,
        *,
        on_submit: Callable[[str], None],
        on_cancel: Callable[[], None],
        on_closed: Callable[[], None],
    ) -> None:
        super().__init__()
        self._on_submit = on_submit
        self._on_closed = on_closed
        self.setWindowTitle("L.U.N.A.")
        self.resize(440, 540)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.input = _InputLine(on_cancel)
        self.input.setPlaceholderText("Type a command and press Enter")
        self.input.returnPressed.connect(self._submit)
        self.status = QLabel("")
        layout = QVBoxLayout(self)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.input)
        layout.addWidget(self.status)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self._on_submit(text)

    def append(self, line: ConversationLine) -> None:
        self.log.appendHtml(self._styled(line.kind, _PREFIX[line.kind] + line.text))

    @staticmethod
    def _styled(kind: str, text: str) -> str:
        escaped = html.escape(text).replace("\n", "<br>")
        if kind == "user":
            return f"<b>{escaped}</b>"
        if kind == "action":
            return f"<span style='color:#6b7280'>{escaped}</span>"
        if kind == "system":
            return f"<i style='color:#9ca3af'>{escaped}</i>"
        return f"<span>{escaped}</span>"

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_input_busy(self, busy: bool) -> None:
        self.input.setEnabled(not busy)
        if not busy and self.isVisible():
            self.input.setFocus()

    def show(self) -> None:
        super().show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    @property
    def visible(self) -> bool:
        return self.isVisible()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
        self._on_closed()
