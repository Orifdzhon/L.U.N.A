"""Starfield backdrop and first-run guide, on the offscreen Qt platform."""

from __future__ import annotations

import random

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from luna.ui.qt.guide import GUIDE_KEYS
from luna.ui.qt.starfield import (
    LAYERS,
    SHOOTING_MIN_GAP_S,
    StarfieldWidget,
    make_stars,
    star_alpha,
    star_position,
)
from luna.ui.qt.window import QtWindowView
from luna.ui.shell.strings import catalog, t


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _window(**kw):
    return QtWindowView(
        on_submit=lambda _t: None, on_cancel=lambda: None, on_closed=lambda: None, **kw
    )


def test_star_count_layers_and_ranges():
    stars = make_stars(random.Random(1))
    assert len(stars) == sum(layer.count for layer in LAYERS)
    assert 80 <= len(stars) <= 150
    assert all(0 < s.alpha <= 1 and 0 <= s.x <= 1 and 0 <= s.y <= 1 for s in stars)
    assert len({round(s.speed, 1) for s in stars}) > 3  # layers move at different speeds


def test_stars_drift_wrap_and_twinkle_within_bounds():
    star = make_stars(random.Random(2))[0]
    assert star_position(star, 400, 600, 0.0) != star_position(star, 400, 600, 10.0)
    for seconds in (0, 100, 1e4):
        x, y = star_position(star, 400, 600, seconds)
        assert 0 <= x < 400 and 0 <= y < 600
        assert 0 < star_alpha(star, seconds) <= star.alpha
    assert star_position(star, 0, 0, 5.0) == (0.0, 0.0)  # degenerate size: no division by zero


def test_renders_gradient_and_stars(app):
    field = StarfieldWidget(animated=False, seed=3)
    field.resize(200, 300)
    image = QImage(200, 300, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    field.paint_scene(painter)
    painter.end()
    top, bottom = image.pixelColor(0, 0), image.pixelColor(0, 299)
    assert top.red() < bottom.red() < 40  # black -> dark grey, never bright
    bright = sum(1 for x in range(200) for y in range(300) if image.pixelColor(x, y).red() > 60)
    assert bright > 20  # stars are there


def test_timer_follows_visibility_and_animations(app):
    field = StarfieldWidget(seed=4)
    assert not field.animation_active
    field.show()
    assert field.animation_active
    field.hide()
    assert not field.animation_active
    field.show()
    field.set_animated(False)
    assert not field.animation_active
    field.set_animated(True)
    assert field.animation_active
    field.hide()


def test_minimized_window_pauses_the_starfield(app, monkeypatch):
    win = _window()
    win.show()
    assert win.starfield.animation_active
    monkeypatch.setattr(type(win), "isMinimized", lambda self: True)
    win.starfield.eventFilter(win, QEvent(QEvent.Type.WindowStateChange))
    assert not win.starfield.animation_active
    monkeypatch.undo()
    win.hide()


def test_animations_off_is_static_everywhere(app):
    win = _window(animations=False)
    win.show()
    assert not win.starfield.animation_active
    assert not win.orb.animation_active
    assert not any(t.isActive() for t in win.findChildren(QTimer))
    win.hide()


def test_shooting_star_appears_rarely_and_ends(app):
    field = StarfieldWidget(seed=5)
    field.resize(300, 400)
    field.advance(SHOOTING_MIN_GAP_S - 1)
    assert not field.shooting_star_active
    field.advance(120.0)
    assert field.shooting_star_active
    image = QImage(300, 400, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    field.paint_scene(painter)  # draws the comet without error
    painter.end()
    field.advance(122.0)
    assert not field.shooting_star_active


def test_starfield_is_behind_and_fills_the_window(app):
    win = _window()
    win.show()
    siblings = win.children()
    assert siblings.index(win.starfield) < siblings.index(win.orb)
    assert win.starfield.geometry() == win.rect()
    win.resize(500, 700)
    assert win.starfield.geometry() == win.rect()
    win.hide()


def test_guide_hidden_until_requested_then_button_calls_done(app):
    done: list = []
    win = _window(on_guide_done=lambda: done.append(1))
    win.show()
    assert win.guide.isHidden()
    win.show_guide()
    assert win.guide.isVisible()
    assert win.guide.geometry() == win.rect()
    win.guide.button.click()
    assert win.guide.isHidden() and done == [1]
    win.hide()


def test_guide_strings_exist_in_both_languages_and_render(app):
    for key in (*GUIDE_KEYS, "guide.title", "guide.ok", "tray.guide"):
        assert catalog()[key]["ru"] and catalog()[key]["en"]
    win = _window(lang="ru")
    assert win.guide.title.text() == t("guide.title", "ru")
    assert win.guide.button.text() == "Понятно"
    assert [line.text() for line in win.guide.lines] == [t(k, "ru") for k in GUIDE_KEYS]
