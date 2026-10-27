"""run_tray end to end on the offscreen platform with fakes (no tray, mic, models, Startup)."""

from __future__ import annotations

import argparse

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from luna.config.settings import Settings
from luna.core.events import EventBus
from luna.ui.qt.app import run_tray


class FakeVoice:
    wake_state = "OFF"

    def __init__(self, enabled=True):
        self.enabled = enabled

    def set_sinks(self, *args):
        pass


class FakeApp:
    def __init__(self, voice=None):
        self.bus = EventBus()
        self.voice_input = voice
        self.speaker = None
        self.llm_available = True
        self.warnings = []
        self.closed = 0

    def startup_lines(self):
        return ["LLM providers: fake"]

    def close(self):
        self.closed += 1


class FakeInstance:
    def __init__(self):
        self.released = 0

    def acquire(self):
        return True

    def release(self):
        self.released += 1


class FakeAutostart:
    def is_enabled(self):
        return False

    def enable(self, command):
        raise AssertionError("must not be called")

    def disable(self):
        raise AssertionError("must not be called")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def run(qapp, monkeypatch, *, tray=True, minimized=False, settings=None, voice=None, steps=None):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: tray))
    fake = FakeApp(voice)
    instance = FakeInstance()
    seen = {}
    factory_kwargs = {}

    def factory(s, **kw):
        factory_kwargs.update(kw)
        return fake

    def ready(qt, controller):
        seen["visible_at_start"] = controller._window.visible
        QTimer.singleShot(50, lambda: steps(controller) if steps else None)
        QTimer.singleShot(150, controller.quit)

    args = argparse.Namespace(minimized=minimized, config=None)
    rc = run_tray(
        settings or Settings(),
        args,
        instance=instance,
        autostart=FakeAutostart(),
        app_factory=factory,
        watchdog=lambda: None,
        on_ready=ready,
    )
    return rc, fake, instance, seen, factory_kwargs


def test_start_show_hide_and_quit(qapp, monkeypatch):
    checks = []

    def steps(controller):
        controller.toggle_window()
        checks.append(controller._window.visible)
        controller.toggle_window()
        checks.append(controller._window.visible)

    rc, fake, instance, seen, kw = run(qapp, monkeypatch, steps=steps)
    assert rc == 0
    assert fake.closed == 1 and instance.released == 1
    assert kw == {"start_wake": False}
    assert seen["visible_at_start"] is True
    assert checks == [False, True]
    assert qapp.quitOnLastWindowClosed() is False


def test_minimized_flag_and_config_hide_window(qapp, monkeypatch):
    _, _, _, seen, _ = run(qapp, monkeypatch, minimized=True)
    assert seen["visible_at_start"] is False
    settings = Settings()
    settings.ui.start_minimized = True
    _, _, _, seen, _ = run(qapp, monkeypatch, settings=settings)
    assert seen["visible_at_start"] is False


def test_no_system_tray_keeps_window_visible(qapp, monkeypatch):
    _, fake, _, seen, _ = run(qapp, monkeypatch, tray=False, minimized=True)
    assert seen["visible_at_start"] is True
    assert fake.closed == 1


def test_wake_requested_only_when_enabled_and_voice_up(qapp, monkeypatch):
    settings = Settings()
    settings.wakeword.enabled = True
    started = []
    monkeypatch.setattr(
        "luna.ui.shell.controller.ShellController.start",
        lambda self, *, wake, minimized: started.append(wake),
    )
    run(qapp, monkeypatch, settings=settings, voice=FakeVoice(enabled=True))
    run(qapp, monkeypatch, settings=settings, voice=FakeVoice(enabled=False))
    run(qapp, monkeypatch, settings=settings, voice=None)
    assert started == [True, False, False]


class FakeKeysDialog:
    """Stands in for the real dialog: records that it was shown."""

    shown = 0

    def __init__(self, presenter, *args, **kwargs):
        pass

    def show(self):
        type(self).shown += 1

    def raise_(self):
        pass

    def activateWindow(self):
        pass

    def close(self):
        pass


def test_keys_dialog_opens_on_start_without_cloud_key(qapp, monkeypatch):
    FakeKeysDialog.shown = 0
    monkeypatch.setattr("luna.ui.qt.app.KeysDialog", FakeKeysDialog)
    run(qapp, monkeypatch, steps=lambda c: None)
    assert FakeKeysDialog.shown == 1


def test_no_keys_dialog_when_a_cloud_key_exists(qapp, monkeypatch):
    FakeKeysDialog.shown = 0
    monkeypatch.setattr("luna.ui.qt.app.KeysDialog", FakeKeysDialog)
    settings = Settings().with_api_keys({"GROQ_API_KEY": "gk-present-1"})
    run(qapp, monkeypatch, settings=settings, steps=lambda c: None)
    assert FakeKeysDialog.shown == 0
