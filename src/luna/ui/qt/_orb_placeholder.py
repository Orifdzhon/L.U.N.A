"""Static stand-in for the animated orb (same API as ``luna.ui.qt.orb.OrbWidget``)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QRadialGradient
from PySide6.QtWidgets import QWidget

from luna.ui.shell.state import IconState

_GLOW = {
    IconState.IDLE: "#8FA3C8",
    IconState.WAKE: "#6EA8FE",
    IconState.RECORDING: "#FF5C7A",
    IconState.THINKING: "#F5B84B",
    IconState.SPEAKING: "#5EE6A8",
    IconState.ERROR: "#FF4D4F",
}


class OrbWidget(QWidget):
    """A moon disc with a state-coloured halo. No timers: nothing runs while hidden."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = IconState.IDLE
        self.setFixedSize(180, 180)

    @property
    def state(self) -> IconState:
        return self._state

    def set_state(self, state: IconState) -> None:
        if state is not self._state:
            self._state = state
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            center = self.rect().center()
            glow = QColor(_GLOW[self._state])
            halo = QRadialGradient(center, 88)
            glow.setAlpha(140)
            halo.setColorAt(0.55, glow)
            glow.setAlpha(0)
            halo.setColorAt(1.0, glow)
            painter.setBrush(halo)
            painter.drawEllipse(QRectF(self.rect()))
            core = QRadialGradient(center, 56)
            core.setColorAt(0.0, QColor("#E8F0FF"))
            core.setColorAt(1.0, QColor("#B8C7E6"))
            painter.setBrush(core)
            painter.drawEllipse(QRectF(center.x() - 56, center.y() - 56, 112, 112))
        finally:
            painter.end()
