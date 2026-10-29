"""The moon orb widget (ADR 0011 Feature C): glow colour and motion follow ``IconState``.

Self-contained: the window just adds it to a layout and calls ``set_state``. One ``QTimer``
drives the animation; it runs only while the widget is visible, its window is not minimized
and animations are on, so a hidden window costs no CPU. ``paint_orb`` is reusable for icons.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QPainter,
    QPaintEvent,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from luna.ui.shell.orb import (
    CROSSFADE_S,
    MOON_CORE,
    OrbFrame,
    OrbStyle,
    mix_color,
    orb_frame,
    orb_style,
)
from luna.ui.shell.state import IconState

_SIZE = 180
_CRATERS = ((-0.32, -0.20, 0.22, 0.16), (0.25, 0.10, 0.17, 0.13), (-0.05, 0.38, 0.14, 0.10))


def _rgba(color: str, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(min(max(alpha, 0.0), 1.0))
    return c


def paint_orb(
    painter: QPainter, rect: QRectF, color: str, frame: OrbFrame, mode: str = "breathe"
) -> None:
    """Draw the moon, glow rings, THINKING arc and ripples for one frame inside ``rect``."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    centre = rect.center()
    moon = min(rect.width(), rect.height()) * 0.27 * frame.radius
    unit = min(rect.width(), rect.height()) * 0.27  # resting moon radius

    glow_r = unit * 1.9
    glow = QRadialGradient(centre, glow_r)
    glow.setColorAt(0.0, _rgba(color, 0.75 * frame.glow_alpha))
    glow.setColorAt(0.5, _rgba(color, 0.25 * frame.glow_alpha))
    glow.setColorAt(1.0, _rgba(color, 0.0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(glow))
    painter.drawEllipse(centre, glow_r, glow_r)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    for factor, share in ((1.25, 0.6), (1.55, 0.3)):  # two soft rings
        painter.setPen(QPen(_rgba(color, share * frame.glow_alpha), 2))
        painter.drawEllipse(centre, moon * factor, moon * factor)

    for progress in frame.ripple_radii:
        radius = unit * (1.15 + 0.75 * progress)
        painter.setPen(QPen(_rgba(color, 0.8 * (1.0 - progress)), 2))
        painter.drawEllipse(centre, radius, radius)

    if mode == "spin":
        arc_r = moon * 1.35
        gradient = QConicalGradient(centre, -frame.arc_angle)
        gradient.setColorAt(0.0, _rgba(color, 1.0))
        gradient.setColorAt(0.3, _rgba(color, 0.0))
        gradient.setColorAt(1.0, _rgba(color, 0.0))
        pen = QPen(QBrush(gradient), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(
            QRectF(centre.x() - arc_r, centre.y() - arc_r, 2 * arc_r, 2 * arc_r),
            int(frame.arc_angle * 16),
            -110 * 16,
        )

    body = QRadialGradient(QPointF(centre.x() - moon * 0.3, centre.y() - moon * 0.35), moon * 1.5)
    body.setColorAt(0.0, QColor(MOON_CORE[0]))
    body.setColorAt(1.0, QColor(MOON_CORE[1]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(body))
    painter.drawEllipse(centre, moon, moon)

    painter.setBrush(QColor(90, 105, 140, 38))  # soft crater texture
    for dx, dy, rx, ry in _CRATERS:
        painter.drawEllipse(
            QPointF(centre.x() + dx * moon, centre.y() + dy * moon), rx * moon, ry * moon
        )


class OrbWidget(QWidget):
    """Animated moon. UI thread only; needs a ``QApplication``."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        animated: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._animated = animated
        self._state = IconState.IDLE
        self._style: OrbStyle = orb_style(self._state)
        self._since = clock()  # when the current state started
        self._from_color = self._style.color  # colour at the moment of the last change
        self._frame = orb_frame(self._style, 0.0)
        self._color = self._style.color
        self._window: QObject | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setMinimumSize(120, 120)

    def sizeHint(self) -> QSize:
        return QSize(_SIZE, _SIZE)

    @property
    def state(self) -> IconState:
        return self._state

    @property
    def animation_active(self) -> bool:
        return self._timer.isActive()

    def set_state(self, state: IconState) -> None:
        if state is self._state:
            return
        self._from_color = self._color
        self._state = state
        self._style = orb_style(state)
        self._since = self._clock()
        if self._animated:
            self._timer.setInterval(max(1, round(1000 / self._style.fps)))
            self._tick()
        else:
            self._color = self._style.color
            self._frame = orb_frame(self._style, 0.0)
            self.update()

    def set_animated(self, animated: bool) -> None:
        """Switch motion on/off; off leaves one static frame and stops the timer."""
        self._animated = animated
        if not animated:
            self._color = self._style.color
            self._frame = orb_frame(self._style, 0.0)
            self.update()
        self._sync_timer()

    def _should_run(self) -> bool:
        window = self.window()
        return (
            self._animated
            and self.isVisible()
            and not (window is not None and window.isMinimized())
        )

    def _sync_timer(self) -> None:
        if self._should_run():
            self._timer.setInterval(max(1, round(1000 / self._style.fps)))
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        elapsed = self._clock() - self._since
        self._frame = orb_frame(self._style, elapsed)
        fade = elapsed / CROSSFADE_S
        self._color = mix_color(self._from_color, self._style.color, fade)
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        window = self.window()
        if window is not self._window:
            if self._window is not None:
                self._window.removeEventFilter(self)
            self._window = window
            if window is not self:
                window.installEventFilter(self)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window and event.type() == QEvent.Type.WindowStateChange:
            self._sync_timer()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            paint_orb(painter, QRectF(self.rect()), self._color, self._frame, self._style.mode)
        finally:
            painter.end()
