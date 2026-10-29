"""The first-run guide: a dimmed overlay over the window with a card of short instructions."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from luna.ui.qt.theme import guide_stylesheet
from luna.ui.shell.strings import t

GUIDE_KEYS = ("guide.call", "guide.examples", "guide.conversation", "guide.settings", "guide.again")


class GuideOverlay(QWidget):
    """Covers its parent (the window sets the geometry); the button calls ``on_done``."""

    def __init__(self, parent: QWidget, *, lang: str, on_done: Callable[[], None]) -> None:
        super().__init__(parent)
        self._on_done = on_done
        self.setObjectName("guide-overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(guide_stylesheet())
        card = QFrame()
        card.setObjectName("guide-card")
        column = QVBoxLayout(card)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(10)
        self.title = QLabel(t("guide.title", lang))
        self.title.setObjectName("guide-title")
        self.title.setWordWrap(True)
        column.addWidget(self.title)
        self.lines: list[QLabel] = []
        for key in GUIDE_KEYS:
            line = QLabel(t(key, lang))
            line.setObjectName("guide-line")
            line.setTextFormat(Qt.TextFormat.PlainText)
            line.setWordWrap(True)
            self.lines.append(line)
            column.addWidget(line)
        self.button = QPushButton(t("guide.ok", lang))
        self.button.setObjectName("guide-ok")
        self.button.setDefault(True)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self._done)
        column.addSpacing(4)
        column.addWidget(self.button, 0, Qt.AlignmentFlag.AlignRight)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.addStretch(1)
        outer.addWidget(card)
        outer.addStretch(1)
        self.hide()

    def _done(self) -> None:
        self.hide()
        self._on_done()

    def open(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self.button.setFocus()
