"""Shared fakes and fixtures for the wake-mode wiring tests (loaded as a pytest plugin)."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from fakes import (
    FakeAppCatalog,
    FakeBrowser,
    FakeHotkeyListener,
    FakeLauncher,
    FakeLLM,
    FakeSearch,
    FakeSTT,
    FakeWakeWord,
)
from luna.app import build_app
from luna.config.settings import Settings
from luna.platform.base import AppEntry


class FakeStore:
    def __init__(self, error=None, present=True):
        self.error = error
        self.present = present
        self.ensure_calls = 0

    def is_present(self):
        return self.present

    def ensure(self):
        self.ensure_calls += 1
        if self.error is not None:
            raise self.error
        self.present = True
        return Path("models") / "vosk-fake"


class SpotterFactory:
    def __init__(self, error=None):
        self.error = error
        self.made = []

    def __call__(self, path):
        if self.error is not None:
            raise self.error
        spotter = FakeWakeWord(marker=False)
        self.made.append((path, spotter))
        return spotter


class FakeLocal:
    """Stands in for FasterWhisperSTT: counts model loads behind a lock like the real one."""

    name = "local"
    instances: ClassVar[list] = []

    def __init__(self, *args):
        self.args = args
        self.loads = 0
        self.warm_up_error = None
        self._lock = threading.Lock()
        self._loaded = False
        FakeLocal.instances.append(self)

    def warm_up(self):
        if self.warm_up_error is not None:
            raise self.warm_up_error
        with self._lock:
            if not self._loaded:
                time.sleep(0.05)
                self.loads += 1
                self._loaded = True

    def start_warm_up(self):
        thread = threading.Thread(target=self.warm_up, name="stt-warm-up", daemon=True)
        thread.start()
        return thread

    def transcribe_short(self, pcm16, language=None):
        return SimpleNamespace(text="")


class FakeSpeaker:
    def __init__(self):
        self.active = False
        self.muted = False
        self.listeners = []

    def add_activity_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback) if callback in self.listeners else None

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_local(monkeypatch):
    FakeLocal.instances = []
    monkeypatch.setattr("luna.voice.input.FasterWhisperSTT", FakeLocal)
    return FakeLocal


@pytest.fixture
def wake_apps():
    made = []
    yield made
    for app in made:
        app.close()


def make(tmp_path, apps, *, settings=None, wake=False, **kw):
    if settings is None:
        settings = Settings().with_api_keys({})
        settings.wakeword.verify = "local"  # the project default is "off" (#170); tests use stage 2
    settings.wakeword.enabled = wake
    kw.setdefault("stt", FakeSTT([]))
    kw.setdefault("hotkey", FakeHotkeyListener())
    kw.setdefault("wake_store", FakeStore())
    kw.setdefault("spotter_factory", SpotterFactory())
    catalog = FakeAppCatalog([AppEntry("Telegram", "Telegram.TelegramDesktop")])
    app = build_app(
        settings,
        llm=FakeLLM([]),
        browser=FakeBrowser(),
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        search=FakeSearch(),
        log_dir=tmp_path,
        **kw,
    )
    apps.append(app)
    return app


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def wake_threads():
    return [t for t in threading.enumerate() if t.name == "luna-wake" and t.is_alive()]
