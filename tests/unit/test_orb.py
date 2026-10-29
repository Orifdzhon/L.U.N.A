"""Orb model (pure) and ``OrbWidget`` (offscreen Qt)."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from luna.ui.qt.orb import OrbWidget, paint_orb
from luna.ui.shell.orb import mix_color, orb_frame, orb_style
from luna.ui.shell.state import IconState

HEX = r"^#[0-9A-F]{6}$"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_style_table_covers_every_state() -> None:
    import re

    expected = {
        IconState.IDLE: ("#8FA3C8", 6.0, "breathe"),
        IconState.WAKE: ("#6EA8FE", 3.0, "breathe"),
        IconState.RECORDING: ("#FF5C7A", 0.9, "pulse"),
        IconState.THINKING: ("#F5B84B", 1.2, "spin"),
        IconState.SPEAKING: ("#5EE6A8", 1.5, "ripple"),
        IconState.ERROR: ("#FF4D4F", 0.3, "flash"),
    }
    for state in IconState:
        style = orb_style(state)
        assert (style.color, style.period_s, style.mode) == expected[state]
        assert re.match(HEX, style.color)
        assert style.fps in (15, 30)


def test_frames_are_deterministic_and_bounded() -> None:
    for state in IconState:
        style = orb_style(state)
        for t in (0.0, 0.1, 0.5, 1.0, 2.9, 17.3):
            assert orb_frame(style, t) == orb_frame(style, t)
            frame = orb_frame(style, t)
            assert 0.9 <= frame.radius <= 1.1
            assert 0.0 <= frame.glow_alpha <= 1.0
            assert all(0.0 <= r <= 1.0 for r in frame.ripple_radii)


def test_breathing_period() -> None:
    style = orb_style(IconState.IDLE)
    assert orb_frame(style, 0.0) == orb_frame(style, style.period_s)
    assert orb_frame(style, 3.0).glow_alpha > orb_frame(style, 0.0).glow_alpha


def test_thinking_arc_turns_once_per_period() -> None:
    style = orb_style(IconState.THINKING)
    assert orb_frame(style, 0.3).arc_angle == pytest.approx(90.0)
    assert orb_frame(style, 0.0).arc_angle == pytest.approx(orb_frame(style, 1.2).arc_angle)


def test_speaking_has_ripples_others_do_not() -> None:
    assert len(orb_frame(orb_style(IconState.SPEAKING), 0.4).ripple_radii) == 3
    assert orb_frame(orb_style(IconState.IDLE), 0.4).ripple_radii == ()


def test_error_flashes_twice_then_stays_dim() -> None:
    style = orb_style(IconState.ERROR)
    assert orb_frame(style, 0.05).glow_alpha == 1.0
    assert orb_frame(style, 0.2).glow_alpha < 0.5
    assert orb_frame(style, 0.35).glow_alpha == 1.0
    assert orb_frame(style, 5.0).glow_alpha == orb_frame(style, 60.0).glow_alpha < 0.5


def test_mix_color() -> None:
    assert mix_color("#000000", "#FFFFFF", 0.0) == "#000000"
    assert mix_color("#000000", "#FFFFFF", 1.0) == "#FFFFFF"
    assert mix_color("#000000", "#FFFFFF", 0.5) == "#808080"
    assert mix_color("#102030", "#102030", 3.0) == "#102030"


@pytest.mark.parametrize("state", list(IconState))
def test_paint_does_not_raise_for_every_state(qapp, state) -> None:
    style = orb_style(state)
    for t in (0.0, 0.25, 1.0):
        image = QImage(180, 180, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        paint_orb(painter, image.rect().toRectF(), style.color, orb_frame(style, t), style.mode)
        painter.end()
        assert image.pixelColor(90, 90).alpha() > 0  # the moon body is drawn


def test_timer_runs_while_visible_and_stops_on_hide(qapp) -> None:
    orb = OrbWidget()
    assert not orb.animation_active  # never shown yet
    orb.show()
    assert orb.animation_active
    orb.hide()
    assert not orb.animation_active
    orb.show()
    assert orb.animation_active
    orb.close()


def test_timer_stops_when_the_window_is_minimized(qapp) -> None:
    host = QWidget()
    orb = OrbWidget(host)
    host.show()
    assert orb.animation_active
    host.showMinimized()
    qapp.processEvents()
    assert not orb.animation_active
    host.showNormal()
    qapp.processEvents()
    assert orb.animation_active
    host.close()


def test_hiding_the_host_stops_the_timer(qapp) -> None:
    host = QWidget()
    orb = OrbWidget(host)
    host.show()
    host.hide()
    assert not orb.animation_active
    host.close()


def test_static_when_animations_are_off(qapp) -> None:
    orb = OrbWidget(animated=False)
    orb.show()
    assert not orb.animation_active
    orb.set_state(IconState.RECORDING)
    assert not orb.animation_active
    assert orb.grab().toImage().width() > 0  # paints a static frame
    orb.set_animated(True)
    assert orb.animation_active
    orb.set_animated(False)
    assert not orb.animation_active
    orb.close()


def test_set_state_changes_interval_and_crossfades(qapp) -> None:
    now = [0.0]
    orb = OrbWidget(clock=lambda: now[0])
    orb.show()
    assert orb.state is IconState.IDLE
    orb.set_state(IconState.RECORDING)
    assert orb.state is IconState.RECORDING
    assert orb._timer.interval() == round(1000 / 30)
    assert orb._color == orb_style(IconState.IDLE).color  # fade has not progressed yet
    now[0] = 0.125
    orb._tick()
    assert orb._color not in (orb_style(IconState.IDLE).color, orb_style(IconState.RECORDING).color)
    now[0] = 1.0
    orb._tick()
    assert orb._color == orb_style(IconState.RECORDING).color
    orb.close()


def test_render_every_state_to_pixmap(qapp) -> None:
    orb = OrbWidget()
    orb.resize(180, 180)
    for state in IconState:
        orb.set_state(state)
        assert not orb.grab().isNull()
    orb.close()
