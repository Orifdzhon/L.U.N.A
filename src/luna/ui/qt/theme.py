"""The dark theme: palette tokens (ADR 0011), one stylesheet, runtime-drawn button icons."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

PALETTE = {
    "bg": "#050506",  # top of the space gradient
    "bg_bottom": "#121214",  # bottom of the space gradient
    "surface": "#1A1B1E",
    "surface_2": "#25272B",
    "border": "#34363B",
    "text": "#E8E9EC",
    "text_muted": "#9A9DA5",
    "user_bubble": "#30343D",
    "user_text": "#F2F4FF",
    "accent": "#8AB4FF",
    "chip_ok": "#101C1B",
    "chip_ok_text": "#7FD1C7",
    "chip_fail": "#241415",
    "chip_fail_text": "#F28B82",
    "rec": "#FF5C7A",
}

# Surfaces are slightly see-through so the stars show faintly behind bubbles and the input.
SURFACE_ALPHA = 0.86
BUBBLE_ALPHA = 0.88


def rgba(color: str, alpha: float) -> str:
    """``#RRGGBB`` + opacity 0..1 -> a Qt stylesheet ``rgba(r, g, b, a)`` value."""
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {round(alpha * 255)})"


FONT_FAMILY = '"Segoe UI Variable", "Segoe UI", sans-serif'


def stylesheet() -> str:
    p = PALETTE
    sf = rgba(p["surface"], SURFACE_ALPHA)
    ub = rgba(p["user_bubble"], BUBBLE_ALPHA)
    return f"""
QWidget#luna-window {{ background: {p["bg"]}; }}
QWidget#chat-body, QScrollArea#chat, QScrollArea#chat > QWidget#qt_scrollarea_viewport {{
    background: transparent; border: none;
}}
QWidget {{ font-family: {FONT_FAMILY}; color: {p["text"]}; font-size: 10pt; }}
QLabel[bubble="user"] {{
    background: {ub}; color: {p["user_text"]}; border-radius: 14px; padding: 8px 12px;
}}
QLabel[bubble="assistant"] {{
    background: {sf}; border: 1px solid {p["border"]}; border-radius: 14px;
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
QLabel#caption, QLabel#status {{ color: {p["text_muted"]}; }}
QLabel#caption {{ font-size: 11pt; }}
QLabel#status {{ font-size: 9pt; }}
QLineEdit {{
    background: {sf}; border: 1px solid {p["border"]}; border-radius: 12px;
    padding: 8px 12px; selection-background-color: {p["user_bubble"]};
}}
QLineEdit:focus {{ border-color: {p["accent"]}; }}
QLineEdit:disabled {{ color: {p["text_muted"]}; }}
QToolButton {{
    background: {sf}; border: 1px solid {p["border"]}; border-radius: 18px;
}}
QToolButton:hover {{ background: {p["surface_2"]}; }}
QToolButton:disabled {{ background: {sf}; }}
QToolButton#send {{ background: {p["accent"]}; border-color: {p["accent"]}; }}
QToolButton#send:disabled {{ background: {p["surface_2"]}; border-color: {p["border"]}; }}
QToolButton#mic[recording="true"] {{ border-color: {p["rec"]}; background: {p["surface_2"]}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
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


def guide_stylesheet() -> str:
    """Styles of the first-run guide overlay (``guide.py``)."""
    p = PALETTE
    return f"""
QWidget#guide-overlay {{ background: rgba(0, 0, 0, 150); }}
QFrame#guide-card {{
    background: {rgba(p["surface"], 0.94)}; border: 1px solid {p["border"]}; border-radius: 16px;
}}
QLabel#guide-title {{ font-size: 13pt; font-weight: 600; }}
QLabel#guide-line {{ color: {p["text"]}; }}
QPushButton#guide-ok {{
    background: {p["accent"]}; color: {p["bg"]}; border: none; border-radius: 12px;
    padding: 8px 18px; font-weight: 600;
}}
QPushButton#guide-ok:hover {{ background: #A5C5FF; }}
"""
