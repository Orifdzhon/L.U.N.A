import sys
import threading
import types

import pytest

from fakes import (
    FakeAppCatalog,
    FakeBrowser,
    FakeLauncher,
    FakeLLM,
    FakePlayer,
    FakeSearch,
    FakeTTS,
)
from luna.app import build_app
from luna.config.settings import Settings
from luna.platform.base import AppEntry


def build(tmp_path, enabled=True, **kw):
    settings = Settings().with_api_keys({})
    settings.tts.enabled = enabled
    settings.search.fetch_pages = 0
    catalog = FakeAppCatalog([AppEntry("Telegram", "Telegram.TelegramDesktop")])
    return build_app(
        settings,
        llm=FakeLLM([]),
        browser=FakeBrowser(),
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        search=FakeSearch(),
        log_dir=tmp_path / "logs",
        **kw,
    )


@pytest.fixture
def made(tmp_path):
    apps = []

    def factory(**kw):
        app = build(tmp_path, **kw)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()


def test_voice_off_builds_nothing_and_touches_no_audio(made, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # importing it would raise
    app = made(enabled=False)
    assert app.speaker is None
    assert any(line.startswith("Voice: off") for line in app.startup_lines())


def test_voice_on_with_fakes_builds_speaker_and_closes_it(tmp_path):
    player = FakePlayer()
    app = build(tmp_path, tts=FakeTTS(), player=player)
    assert app.speaker is not None
    assert "Voice: on (/mute, /stop, /voice)." in app.startup_lines()
    app.close()
    assert player.closed
    assert not any(t.name.startswith("luna-speaker") for t in threading.enumerate())


def test_voice_on_non_windows_degrades_with_warning(made):
    app = made(platform="linux")
    assert app.speaker is None
    assert any(w.startswith("Voice disabled") for w in app.warnings)
    assert any("Voice disabled" in line for line in app.startup_lines())


def test_voice_on_without_sapi_voices_degrades(made, monkeypatch):
    class NoVoices:
        def __init__(self, *a, **k): ...

        def list_voices(self):
            return ()

    monkeypatch.setattr("luna.tts.sapi.SapiTTS", NoVoices)
    app = made(platform="win32", player=FakePlayer())
    assert app.speaker is None
    assert any("no SAPI voices" in w for w in app.warnings)


def test_voice_on_missing_audio_extra_degrades_with_hint(made, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    app = made(tts=FakeTTS())
    assert app.speaker is None
    assert any("uv sync" in w for w in app.warnings)


def test_voice_on_no_output_device_degrades(made, monkeypatch):
    def no_device(**_kw):
        raise RuntimeError("no device")

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(query_devices=no_device))
    app = made(tts=FakeTTS())
    assert app.speaker is None
    assert any("no audio output device" in w for w in app.warnings)
