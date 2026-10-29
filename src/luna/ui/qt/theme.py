"""The dark theme: palette tokens (ADR 0011), one stylesheet, runtime-drawn button icons."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

PALETTE = {
    "bg": "#0E1016",
    "surface": "#161A23",
    "surface_2": "#1E2330",
    "border": "#2A3040",
    "text": "#E6E8EE",
    "text_muted": "#8B93A7",
    "user_bubble": "#2B3A67",
    "user_text": "#F2F4FF",
    "accent": "#8AB4FF",
    "chip_ok": "#12262A",
    "chip_ok_text": "#7FD1C7",
    "chip_fail": "#2A1416",
    "chip_fail_text": "#F28B82",
    "rec": "#FF5C7A",
}

FONT_FAMILY = '"Segoe UI Variable", "Segoe UI", sans-serif'


def stylesheet() -> str:
    p = PALETTE
    return f"""
QWidget#luna-window, QWidget#chat-body {{ background: {p["bg"]}; }}
QScrollArea#chat {{ background: {p["bg"]}; border: none; }}
QWidget {{ font-family: {FONT_FAMILY}; color: {p["text"]}; font-size: 10pt; }}
QLabel[bubble="user"] {{
    background: {p["user_bubble"]}; color: {p["user_text"]}; border-radius: 14px; padding: 8px 12px;
}}
QLabel[bubble="assistant"] {{
    background: {p["surface"]}; border: 1px solid {p["border"]}; border-radius: 14px;
    padding: 8px 12px;
}}
QLabel[bubble="system"] {{ color: {p["text_muted"]}; font-size: 9pt; }}
QLabel[chip="ok"] {{
    background: {p["chip_ok"]}; color: {p["chip_ok_text"]}; border-radius: 10px; padding: 3px 10px;
    font-size: 9pt;
}}
QLabel[chip="fail"] {{
    background: {p["chip_fail"]}; color: {p["chip_fail_text"]}; border-radius: 10px;
    padding: 3px 10px; font-size: 9pt;
}}
QPushButton[filechip="true"] {{
    background: {p["surface_2"]}; color: {p["accent"]}; border: 1px solid {p["border"]};
    border-radius: 10px; padding: 3px 10px; font-size: 9pt;
}}
QPushButton[filechip="true"]:hover {{ border-color: {p["accent"]}; }}
QLabel[filename="true"] {{ color: {p["text"]}; font-size: 9pt; padding-left: 4px; }}
QLabel#caption, QLabel#status {{ color: {p["text_muted"]}; }}
QLabel#caption {{ font-size: 11pt; }}
QLabel#status {{ font-size: 9pt; }}
QLineEdit {{
    background: {p["surface"]}; border: 1px solid {p["border"]}; border-radius: 12px;
    padding: 8px 12px; selection-background-color: {p["user_bubble"]};
}}
QLineEdit:focus {{ border-color: {p["accent"]}; }}
QLineEdit:disabled {{ color: {p["text_muted"]}; }}
QToolButton {{
    background: {p["surface"]}; border: 1px solid {p["border"]}; border-radius: 18px;
}}
QToolButton:hover {{ background: {p["surface_2"]}; }}
QToolButton:disabled {{ background: {p["bg"]}; }}
QToolButton#send {{ background: {p["accent"]}; border-color: {p["accent"]}; }}
QToolButton#send:disabled {{ background: {p["surface_2"]}; border-color: {p["border"]}; }}
QToolButton#mic[recording="true"] {{ border-color: {p["rec"]}; background: {p["surface_2"]}; }}
QScrollBar:vertical {{ background: {p["bg"]}; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p["border"]}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def send_icon(color: str, size: int = 36) -> QIcon:
    pixmap, painter = _canvas(size)
    try:
        s = size / 36
        path = QPainterPath()
        path.moveTo(QPointF(10 * s, 9 * s))
        path.lineTo(QPointF(28 * s, 18 * s))
        path.lineTo(QPointF(10 * s, 27 * s))
        path.lineTo(QPointF(14 * s, 18 * s))
        path.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPath(path)
    finally:
        painter.end()
    return QIcon(pixmap)


def mic_icon(color: str, size: int = 36) -> QIcon:
    pixmap, painter = _canvas(size)
    try:
        s = size / 36
        pen = QPen(QColor(color), 2 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(14 * s, 7 * s, 8 * s, 14 * s), 4 * s, 4 * s)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(10 * s, 10 * s, 16 * s, 14 * s), 180 * 16, 180 * 16)
        painter.drawLine(QPointF(18 * s, 24 * s), QPointF(18 * s, 29 * s))
        painter.drawLine(QPointF(14 * s, 29 * s), QPointF(22 * s, 29 * s))
    finally:
        painter.end()
    return QIcon(pixmap)
