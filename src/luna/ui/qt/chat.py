"""Chat view: bubbles (user right, Luna left), action chips, file chips. Plain text only."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from luna.ui.shell.state import ConversationLine, FileChip
from luna.ui.shell.strings import t

BUBBLE_WIDTH_RATIO = 0.8
AT_BOTTOM_SLACK_PX = 4
NAME_MAX_CHARS = 30  # file names are cut in the middle; the tooltip has the whole name

OpenFile = Callable[[int, str], None]


def _short(name: str) -> str:
    if len(name) <= NAME_MAX_CHARS:
        return name
    head = (NAME_MAX_CHARS - 1) * 2 // 3
    return name[:head] + "…" + name[-(NAME_MAX_CHARS - 1 - head) :]


def _label(text: str, prop: str, value: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)  # replies and file names never become markup
    label.setProperty(prop, value)
    return label


class ChatView(QScrollArea):
    """UI thread only. ``max_lines`` bounds the widgets kept (the oldest row is deleted)."""

    def __init__(
        self, max_lines: int, on_open_file: OpenFile | None = None, lang: str = "en"
    ) -> None:
        super().__init__()
        self.setObjectName("chat")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._max_lines = max(1, max_lines)
        self._on_open_file = on_open_file
        self._lang = lang
        self._rows: list[QWidget] = []
        self._bubbles: list[QLabel] = []
        self._stick = True  # the view was at the bottom before the last change
        body = QWidget()
        body.setObjectName("chat-body")
        self._column = QVBoxLayout(body)
        self._column.setContentsMargins(12, 8, 12, 8)
        self._column.setSpacing(8)
        self._column.addStretch(1)
        self.setWidget(body)
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_range)

    # ---- model -----------------------------------------------------------------------

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def rows(self) -> list[QWidget]:
        return list(self._rows)

    def add_line(self, line: ConversationLine) -> None:
        self._stick = self._at_bottom()
        row = self._chip_row(line) if line.kind == "action" else self._bubble_row(line)
        self._column.insertWidget(self._column.count() - 1, row)
        self._rows.append(row)
        while len(self._rows) > self._max_lines:
            old = self._rows.pop(0)
            self._bubbles = [b for b in self._bubbles if b.parent() is not old]
            self._column.removeWidget(old)
            old.setParent(None)
            old.deleteLater()
        self._fit_bubbles()

    # ---- rows ------------------------------------------------------------------------

    def _bubble_row(self, line: ConversationLine) -> QWidget:
        row = QWidget()
        row.setProperty("kind", line.kind)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        label = _label(line.text, "bubble", line.kind)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._bubbles.append(label)
        if line.kind == "user":
            layout.addStretch(1)
            layout.addWidget(label)
        elif line.kind == "system":
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label, 1)
        else:
            layout.addWidget(label)
            layout.addStretch(1)
        return row

    def _chip_row(self, line: ConversationLine) -> QWidget:
        row = QWidget()
        row.setProperty("kind", "action")
        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        ok = line.ok is not False  # unknown counts as fine; only an explicit failure is red
        if not line.files:  # a find_file result is shown as its rows, not as a status chip
            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            chip = _label(line.text, "chip", "ok" if ok else "fail")
            chip.setWordWrap(True)
            self._bubbles.append(chip)
            top.addWidget(chip)
            top.addStretch(1)
            column.addLayout(top)
        for file in line.files:
            column.addLayout(self._file_row(file))
        return row

    def _file_row(self, file: FileChip) -> QHBoxLayout:
        """``1. name  [Open] [Folder]``: one compact row per result."""
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        name = _label(f"{file.index}. {_short(file.label)}", "filename", "true")
        if file.label:
            name.setToolTip(file.label)
        layout.addWidget(name)
        layout.addStretch(1)
        if file.kind == "dir":
            layout.addWidget(self._file_button(file, "files.open", "open"))
        elif not file.can_open:
            layout.addWidget(self._file_button(file, "files.reveal", "reveal"))
        else:
            layout.addWidget(self._file_button(file, "files.open", "open"))
            layout.addWidget(self._file_button(file, "files.folder", "reveal"))
        return layout

    def _file_button(self, file: FileChip, key: str, action: str) -> QPushButton:
        button = QPushButton(t(key, self._lang))
        button.setProperty("filechip", "true")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        tip = "files.open_tip" if action == "open" else "files.folder_tip"
        button.setToolTip(t(tip, self._lang, name=file.label or str(file.index)))
        button.clicked.connect(lambda _=False, i=file.index, a=action: self._open(i, a))
        return button

    def _open(self, index: int, action: str) -> None:
        if self._on_open_file is not None:
            self._on_open_file(index, action)

    # ---- scrolling and sizing --------------------------------------------------------

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - AT_BOTTOM_SLACK_PX

    def _on_scrolled(self, _value: int) -> None:
        self._stick = self._at_bottom()

    def _on_range(self, _low: int, high: int) -> None:
        if self._stick:
            self.verticalScrollBar().setValue(high)

    def _fit_bubbles(self) -> None:
        width = int(self.viewport().width() * BUBBLE_WIDTH_RATIO)
        for label in self._bubbles:
            label.setMaximumWidth(max(120, width))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_bubbles()
