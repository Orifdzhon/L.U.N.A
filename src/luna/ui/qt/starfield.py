"""The space backdrop: black-to-dark-grey gradient, three layers of drifting stars, a rare comet.

Sits at the bottom of the window's z-order; the orb and the chat are transparent over it. One
``QTimer`` (about 24 fps) drives it and runs only while the widget is visible, its window is not
minimized and animations are on, like ``OrbWidget``. With animations off one static frame is
drawn. Star motion is a pure function of time, so a frame is cheap: ~110 small ellipses.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QWidget

from luna.ui.qt.theme import PALETTE

FPS = 24
SHOOTING_MIN_GAP_S = 25.0
SHOOTING_MAX_GAP_S = 60.0
SHOOTING_DURATION_S = 0.9
_TINTS = ("#FFFFFF", "#DCE6FF", "#FFF1DC")


@dataclass(frozen=True)
class _Layer:
    count: int
    size: tuple[float, float]  # radius range, logical px
    speed: float  # drift, px per second (nearer layers move faster: parallax)
    alpha: tuple[float, float]


# far, middle, near
LAYERS = (
    _Layer(60, (0.5, 0.8), 1.5, (0.25, 0.55)),
    _Layer(35, (0.7, 1.1), 3.5, (0.4, 0.75)),
    _Layer(15, (1.0, 1.5), 7.0, (0.6, 0.95)),
)


@dataclass(frozen=True)
class Star:
    x: float  # start position, 0..1 of the widget
    y: float
    radius: float
    alpha: float
    speed: float
    phase: float  # twinkle phase, radians
    period: float  # twinkle period, seconds
    tint: str


def make_stars(rng: random.Random) -> list[Star]:
    stars: list[Star] = []
    for layer in LAYERS:
        for _ in range(layer.count):
            stars.append(
                Star(
                    x=rng.random(),
                    y=rng.random(),
                    radius=rng.uniform(*layer.size),
                    alpha=rng.uniform(*layer.alpha),
                    speed=layer.speed * rng.uniform(0.8, 1.2),
                    phase=rng.uniform(0.0, math.tau),
                    period=rng.uniform(3.0, 8.0),
                    tint=rng.choice(_TINTS),
                )
            )
    return stars


def star_position(star: Star, width: float, height: float, t: float) -> tuple[float, float]:
    """Drift to the left and slightly down; wraps around the edges."""
    x = (star.x * width - star.speed * t) % width if width > 0 else 0.0
    y = (star.y * height + star.speed * 0.25 * t) % height if height > 0 else 0.0
    return x, y


def star_alpha(star: Star, t: float) -> float:
    """Soft twinkle: brightness swings between 65 % and 100 % of the base."""
    return star.alpha * (0.825 + 0.175 * math.sin(math.tau * t / star.period + star.phase))


class StarfieldWidget(QWidget):
    """Gradient + stars. UI thread only; needs a ``QApplication``."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        animated: bool = True,
        clock: Callable[[], float] = time.monotonic,
        seed: int | None = None,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._animated = animated
        self._rng = random.Random(seed)
        self._stars = make_stars(self._rng)
        self._origin = clock()
        self._t = 0.0
        self._next_shot = self._schedule_shot(0.0)
        self._shot_started = -1.0
        self._shot_from = QPointF()
        self._window: QObject | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(round(1000 / FPS))
        self._timer.timeout.connect(self._tick)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    @property
    def animation_active(self) -> bool:
        return self._timer.isActive()

    @property
    def star_count(self) -> int:
        return len(self._stars)

    @property
    def shooting_star_active(self) -> bool:
        return self._shot_started >= 0.0

    def set_animated(self, animated: bool) -> None:
        """Motion on/off; off freezes the current frame and stops the timer."""
        self._animated = animated
        if not animated:
            self._shot_started = -1.0
            self.update()
        self._sync_timer()

    def _schedule_shot(self, now: float) -> float:
        return now + self._rng.uniform(SHOOTING_MIN_GAP_S, SHOOTING_MAX_GAP_S)

    def _should_run(self) -> bool:
        window = self.window()
        return (
            self._animated
            and self.isVisible()
            and not (window is not None and window.isMinimized())
        )

    def _sync_timer(self) -> None:
        if self._should_run():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        self.advance(self._clock() - self._origin)
        self.update()

    def advance(self, t: float) -> None:
        """Move the scene clock to ``t`` seconds and start or end the shooting star."""
        self._t = t
        if self._shot_started >= 0.0 and t - self._shot_started > SHOOTING_DURATION_S:
            self._shot_started = -1.0
            self._next_shot = self._schedule_shot(t)
        elif self._shot_started < 0.0 and t >= self._next_shot:
            self._shot_started = t
            w, h = max(1, self.width()), max(1, self.height())
            self._shot_from = QPointF(
                self._rng.uniform(0.35, 0.95) * w, self._rng.uniform(0.02, 0.3) * h
            )

    # ---- events ----------------------------------------------------------------------

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
        if watched is getattr(self, "_window", None) and (
            event.type() == QEvent.Type.WindowStateChange
        ):
            self._sync_timer()
        return False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            self.paint_scene(painter)
        finally:
            painter.end()

    def paint_scene(self, painter: QPainter) -> None:
        w, h = float(self.width()), float(self.height())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        sky = QLinearGradient(0.0, 0.0, 0.0, h)
        sky.setColorAt(0.0, QColor(PALETTE["bg"]))
        sky.setColorAt(1.0, QColor(PALETTE["bg_bottom"]))
        painter.fillRect(self.rect(), sky)
        painter.setPen(Qt.PenStyle.NoPen)
        for star in self._stars:
            x, y = star_position(star, w, h, self._t)
            colour = QColor(star.tint)
            colour.setAlphaF(min(1.0, max(0.0, star_alpha(star, self._t))))
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(x, y), star.radius, star.radius)
        if self._shot_started >= 0.0:
            self._paint_shooting_star(painter)

    def _paint_shooting_star(self, painter: QPainter) -> None:
        progress = min(1.0, (self._t - self._shot_started) / SHOOTING_DURATION_S)
        head = self._shot_from + QPointF(-1.0, 0.55) * (progress * 160.0)
        tail = head + QPointF(1.0, -0.55) * 46.0
        fade = math.sin(math.pi * progress)  # in and out
        gradient = QLinearGradient(head, tail)
        bright = QColor("#FFFFFF")
        bright.setAlphaF(0.85 * fade)
        clear = QColor("#FFFFFF")
        clear.setAlphaF(0.0)
        gradient.setColorAt(0.0, bright)
        gradient.setColorAt(1.0, clear)
        pen = QPen(gradient, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(head, tail)
