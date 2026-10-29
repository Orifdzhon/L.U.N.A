"""Offscreen smoke tests for the Qt views (no real tray, no display)."""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QSystemTrayIcon

from luna.ui.qt import window as window_module
from luna.ui.qt.dispatch import QtUiThread
from luna.ui.qt.icons import make_icon
from luna.ui.qt.tray import QtTrayView
from luna.ui.qt.window import QtWindowView
from luna.ui.shell.state import ConversationLine, FileChip, IconState, TrayMenuState


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_every_icon_state_is_non_null_and_distinct(app):
    images = {}
    for state in IconState:
        icon = make_icon(state)
        assert not icon.isNull()
        images[state] = icon.pixmap(64, 64).toImage()
    states = list(IconState)
    for i, a in enumerate(states):
        for b in states[i + 1 :]:
            assert images[a] != images[b], (a, b)


def test_post_from_other_thread_runs_on_gui_thread(app):
    ui = QtUiThread()
    ran: list[threading.Thread] = []
    done = threading.Event()

    def job() -> None:
        ran.append(threading.current_thread())
        done.set()

    worker = threading.Thread(target=lambda: ui.post(job))
    worker.start()
    worker.join()
    assert not ran  # queued, not executed in the worker
    for _ in range(100):
        QCoreApplication.processEvents()
        if done.is_set():
            break
        QTest.qWait(10)
    assert ran == [threading.current_thread()]


def _tray(calls):
    return QtTrayView(
        on_open=lambda: calls.append(("open",)),
        on_set_wake=lambda v: calls.append(("wake", v)),
        on_set_voice=lambda v: calls.append(("voice", v)),
        on_set_autostart=lambda v: calls.append(("autostart", v)),
        on_quit=lambda: calls.append(("quit",)),
        on_keys=lambda: calls.append(("keys",)),
    )


def test_tray_menu_has_six_items_and_reflects_state(app):
    calls: list = []
    tray = _tray(calls)
    tray.set_menu(TrayMenuState(True, False, "Starting wake word…", False, True, True, False))
    assert tray.wake_action.text() == "Starting wake word…"
    assert tray.wake_action.isChecked() and not tray.wake_action.isEnabled()
    assert not tray.voice_action.isChecked() and tray.voice_action.isEnabled()
    assert tray.autostart_action.isChecked() and not tray.autostart_action.isEnabled()
    assert calls == []  # programmatic updates do not fire callbacks
    actions = [a for a in tray._menu.actions() if not a.isSeparator()]
    assert len(actions) == 6
    assert tray.keys_action.text() == "Models / providers…"
    assert tray._menu.defaultAction() is tray.open_action


def test_keys_action_invokes_callback(app):
    calls: list = []
    tray = _tray(calls)
    tray.keys_action.trigger()
    assert calls == [("keys",)]


def test_tray_actions_invoke_callbacks(app):
    calls: list = []
    tray = _tray(calls)
    tray.set_menu(TrayMenuState(False, True, "Listen for «Луна»", True, True, False, True))
    tray.wake_action.trigger()
    tray.voice_action.trigger()
    tray.autostart_action.trigger()
    tray.open_action.trigger()
    tray.quit_action.trigger()
    assert calls == [
        ("wake", True),
        ("voice", False),
        ("autostart", True),
        ("open",),
        ("quit",),
    ]


def test_tray_left_click_opens_and_state_and_notify(app, monkeypatch):
    calls: list = []
    tray = _tray(calls)
    tray._on_activated(QSystemTrayIcon.ActivationReason.Context)
    assert calls == []
    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert calls == [("open",)]
    tray.set_state(IconState.ERROR, "tip")
    assert tray._tray.toolTip() == "tip"
    shown = []
    monkeypatch.setattr(tray._tray, "showMessage", lambda *a: shown.append(a[:2]))
    tray.notify("T", "body")
    assert shown == [("T", "body")]


def _window(calls):
    return QtWindowView(
        on_submit=lambda t: calls.append(("submit", t)),
        on_cancel=lambda: calls.append(("cancel",)),
        on_closed=lambda: calls.append(("closed",)),
    )


def _texts(win):
    return [lbl.text() for lbl in win.chat.findChildren(QLabel)]


def test_window_append_status_busy(app):
    win = _window([])
    win.append(ConversationLine("user", "hello <b>"))
    win.append(ConversationLine("assistant", "hi"))
    assert _texts(win) == ["hello <b>", "hi"]  # markup is shown literally
    assert all(lbl.textFormat() == Qt.TextFormat.PlainText for lbl in win.chat.findChildren(QLabel))
    win.set_status("Thinking")
    assert win.status.text() == "Thinking"
    win.set_input_busy(True)
    assert not win.input.isEnabled() and not win.send_button.isEnabled()
    win.set_input_busy(False)
    assert win.input.isEnabled()


def test_bubbles_align_by_kind_and_chip_colour_follows_ok(app):
    win = _window([])
    win.resize(420, 640)
    win.show()
    win.append(ConversationLine("user", "open youtube"))
    win.append(ConversationLine("assistant", "Opening."))
    win.append(ConversationLine("action", "open_website: ok", ok=True))
    win.append(ConversationLine("action", "close_app: failed", ok=False))
    win.append(ConversationLine("system", "note"))
    QApplication.processEvents()
    labels = {lbl.text(): lbl for lbl in win.chat.findChildren(QLabel)}
    assert labels["open youtube"].property("bubble") == "user"
    assert labels["Opening."].property("bubble") == "assistant"
    user_x = labels["open youtube"].mapTo(win, labels["open youtube"].rect().topLeft()).x()
    luna_x = labels["Opening."].mapTo(win, labels["Opening."].rect().topLeft()).x()
    assert user_x > luna_x
    assert labels["open youtube"].geometry().right() > win.width() // 2
    assert labels["open_website: ok"].property("chip") == "ok"
    assert labels["close_app: failed"].property("chip") == "fail"
    assert labels["note"].property("bubble") == "system"
    win.hide()


def test_long_bubble_is_at_most_80_percent_wide(app):
    win = _window([])
    win.show()
    win.append(ConversationLine("assistant", "word " * 200))
    QApplication.processEvents()
    bubble = win.chat.findChildren(QLabel)[0]
    assert bubble.width() <= win.chat.viewport().width() * 0.8 + 1
    win.hide()


def test_chat_is_bounded_by_history_lines(app):
    win = QtWindowView(
        on_submit=lambda t: None, on_cancel=lambda: None, on_closed=lambda: None, history_lines=3
    )
    for i in range(5):
        win.append(ConversationLine("assistant", f"m{i}"))
    assert win.chat.row_count == 3
    assert _texts(win) == ["m2", "m3", "m4"]


def test_autoscroll_only_when_at_bottom(app):
    win = _window([])
    win.show()
    for i in range(40):
        win.append(ConversationLine("assistant", f"line {i}"))
    QTest.qWait(30)
    bar = win.chat.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == bar.maximum()
    bar.setValue(0)  # the user scrolled up
    win.append(ConversationLine("assistant", "new"))
    QTest.qWait(30)
    assert bar.value() == 0
    bar.setValue(bar.maximum())
    win.append(ConversationLine("assistant", "newer"))
    QTest.qWait(30)
    assert bar.value() == bar.maximum()
    win.hide()


def test_file_chips_call_open_file_and_other_actions_have_none(app):
    opened: list = []
    win = QtWindowView(
        on_submit=lambda t: None,
        on_cancel=lambda: None,
        on_closed=lambda: None,
        on_open_file=lambda i, a: opened.append((i, a)),
    )
    win.append(ConversationLine("action", "open_website: ok", ok=True))
    assert not win.chat.findChildren(QPushButton)
    win.append(
        ConversationLine(
            "action", "find_file: ok", ok=True, files=(FileChip(1, "a.pdf"), FileChip(2))
        )
    )
    buttons = {b.text(): b for b in win.chat.findChildren(QPushButton)}
    assert set(buttons) == {"Open 1", "Folder 1", "Open 2", "Folder 2"}
    buttons["Open 2"].click()
    buttons["Folder 1"].click()
    assert opened == [(2, "open"), (1, "reveal")]


def test_send_button_and_enter_submit(app):
    calls: list = []
    win = _window(calls)
    win.show()
    assert not win.send_button.isEnabled()
    win.input.setText("hello")
    assert win.send_button.isEnabled()
    win.send_button.click()
    assert calls == [("submit", "hello")] and win.input.text() == ""
    win.hide()


def test_mic_button_presses_and_follows_state(app):
    calls: list = []
    win = QtWindowView(
        on_submit=lambda t: None,
        on_cancel=lambda: None,
        on_closed=lambda: None,
        on_listen=lambda: calls.append("listen"),
    )
    win.mic_button.click()
    assert calls == ["listen"] and not win.mic_button.isChecked()  # state is the controller's
    win.set_state(IconState.RECORDING, "Listening")
    assert win.mic_button.isChecked() and win.caption.text() == "Listening"
    assert win.orb.state is IconState.RECORDING
    win.mic_button.click()
    assert calls == ["listen", "listen"] and win.mic_button.isChecked()
    win.set_state(IconState.IDLE, "Idle")
    assert not win.mic_button.isChecked()


def test_mic_button_disabled_with_reason(app):
    win = _window([])
    win.set_voice_available(False, "Voice input is not available")
    assert not win.mic_button.isEnabled()
    assert win.mic_button.toolTip() == "Voice input is not available"
    win.set_voice_available(True, "")
    assert win.mic_button.isEnabled() and win.mic_button.toolTip()


def test_hidden_window_runs_no_timers(app):
    win = _window([])
    win.show()
    win.set_state(IconState.THINKING, "Thinking")
    win.hide()
    assert not any(t.isActive() for t in win.findChildren(QTimer))


def test_dark_title_bar_requested_on_show(app, monkeypatch):
    seen: list = []
    monkeypatch.setattr(window_module, "enable_dark_title_bar", lambda hwnd: seen.append(hwnd))
    win = _window([])
    win.show()
    assert len(seen) == 1
    win.hide()


def test_window_enter_submits_and_escape_cancels(app):
    calls: list = []
    win = _window(calls)
    win.show()
    win.input.setText("  open youtube ")
    QTest.keyClick(win.input, Qt.Key.Key_Return)
    assert calls == [("submit", "open youtube")]
    assert win.input.text() == ""
    QTest.keyClick(win.input, Qt.Key.Key_Return)  # empty input: ignored
    QTest.keyClick(win.input, Qt.Key.Key_Escape)
    assert calls == [("submit", "open youtube"), ("cancel",)]
    win.hide()


def test_window_close_hides_without_destroying(app):
    calls: list = []
    win = _window(calls)
    win.show()
    assert win.visible
    event = QCloseEvent()
    QApplication.sendEvent(win, event)
    assert event.type() == QEvent.Type.Close
    assert not event.isAccepted()
    assert not win.visible
    assert calls == [("closed",)]
    win.show()
    assert win.visible
    win.hide()


def test_core_and_shell_do_not_import_pyside6():
    code = (
        "import sys, luna, luna.ui.shell, luna.ui.cli; "
        "sys.exit(any(m.startswith('PySide6') for m in sys.modules))"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
