"""Tray icons drawn at runtime with QPainter (no image files)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

from luna.ui.shell.state import IconState

_SIZE = 64

_COLORS = {
    IconState.IDLE: "#8a8f98",
    IconState.WAKE: "#3b82f6",
    IconState.RECORDING: "#ef4444",
    IconState.THINKING: "#f59e0b",
    IconState.SPEAKING: "#22c55e",
    IconState.ERROR: "#b91c1c",
}


def make_icon(state: IconState) -> QIcon:
    pixmap = QPixmap(_SIZE, _SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_COLORS[state]))
        painter.drawEllipse(QRectF(4, 4, _SIZE - 8, _SIZE - 8))
        if state is IconState.ERROR:
            painter.setPen(QPen(QColor("white")))
            font = QFont()
            font.setPixelSize(40)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(0, 0, _SIZE, _SIZE), Qt.AlignmentFlag.AlignCenter, "!")
    finally:
        painter.end()
    return QIcon(pixmap)


def make_icons() -> dict[IconState, QIcon]:
    return {state: make_icon(state) for state in IconState}
