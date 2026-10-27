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


def build(tmp_path, enabled=True, provider="edge", **kw):
    settings = Settings().with_api_keys({})
    settings.tts.provider = provider
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


@pytest.fixture(autouse=True)
def no_piper_download(monkeypatch):
    """A real PiperTTS built here must never start its (downloading) warm-up."""
    monkeypatch.setattr("luna.tts.piper.PiperTTS.warm_up", lambda self: None)


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


def test_sapi_voice_on_non_windows_degrades_with_warning(made):
    app = made(platform="linux", provider="sapi")
    assert app.speaker is None
    assert any(w.startswith("Voice disabled") for w in app.warnings)
    assert any("Voice disabled" in line for line in app.startup_lines())


def test_sapi_voice_without_sapi_voices_degrades(made, monkeypatch):
    class NoVoices:
        def __init__(self, *a, **k): ...

        def list_voices(self):
            return ()

    monkeypatch.setattr("luna.tts.sapi.SapiTTS", NoVoices)
    app = made(platform="win32", player=FakePlayer(), provider="sapi")
    assert app.speaker is None
    assert any("no SAPI voices" in w for w in app.warnings)


class _FakeSapi:
    name = "sapi"

    def __init__(self, *a, **k): ...

    def list_voices(self):
        return ("Irina",)


def test_edge_voice_on_windows_builds_edge_piper_sapi_chain(made, monkeypatch):
    monkeypatch.setattr("luna.tts.sapi.SapiTTS", _FakeSapi)
    app = made(platform="win32", player=FakePlayer())
    assert app.speaker is not None
    assert app.speaker._tts.name == "edge+piper+sapi"


def test_edge_chain_uses_the_short_first_audio_timeout_and_no_retry(made, monkeypatch):
    monkeypatch.setattr("luna.tts.sapi.SapiTTS", _FakeSapi)
    app = made(platform="win32", player=FakePlayer())
    edge = app.speaker._tts._providers[0]
    assert edge._first_audio_s == 2.5
    assert edge._attempts == 1


def test_edge_first_audio_timeout_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr("luna.tts.sapi.SapiTTS", _FakeSapi)
    settings = Settings().with_api_keys({})
    settings.tts.provider = "edge"
    settings.tts.enabled = True
    settings.tts.edge.first_audio_s = 1.5
    settings.search.fetch_pages = 0
    catalog = FakeAppCatalog([])
    app = build_app(
        settings,
        llm=FakeLLM([]),
        browser=FakeBrowser(),
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        search=FakeSearch(),
        log_dir=tmp_path / "logs",
        platform="win32",
        player=FakePlayer(),
    )
    try:
        assert app.speaker._tts._providers[0]._first_audio_s == 1.5
    finally:
        app.close()


def test_edge_chain_skips_piper_that_cannot_be_built_with_one_warning(made, monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            raise OSError("boom")

    monkeypatch.setattr("luna.tts.sapi.SapiTTS", _FakeSapi)
    monkeypatch.setattr("luna.tts.piper.PiperTTS", Broken)
    app = made(platform="win32", player=FakePlayer())
    assert app.speaker._tts.name == "edge+sapi"
    assert len([w for w in app.warnings if "Piper" in w]) == 1


def test_piper_voice_on_windows_wraps_piper_with_sapi_fallback(made, monkeypatch):
    monkeypatch.setattr("luna.tts.sapi.SapiTTS", _FakeSapi)
    app = made(platform="win32", player=FakePlayer(), provider="piper")
    assert app.speaker is not None
    assert app.speaker._tts.name == "piper+sapi"


def test_piper_voice_without_sapi_has_no_fallback_but_no_network_warning(made):
    app = made(platform="linux", player=FakePlayer(), provider="piper")
    assert app.speaker is not None
    assert app.speaker._tts.name == "piper"
    assert not any("needs the network" in w for w in app.warnings)


def test_piper_settings_reach_the_provider(made, monkeypatch, tmp_path):
    seen = {}

    class Spy:
        name = "piper"

        def __init__(self, voice_ru, voice_en, rate, volume, *, model_dir, num_threads):
            seen.update(ru=voice_ru, en=voice_en, model_dir=model_dir, threads=num_threads)

    monkeypatch.setattr("luna.tts.piper.PiperTTS", Spy)
    app = made(platform="linux", player=FakePlayer(), provider="piper")
    assert app.speaker is not None
    assert seen == {
        "ru": "ru_RU-irina-medium",
        "en": "en_US-kristin-medium",
        "model_dir": None,
        "threads": 2,
    }


def test_edge_voice_without_sapi_keeps_edge_and_piper(made, monkeypatch):
    class NoVoices(_FakeSapi):
        def list_voices(self):
            return ()

    monkeypatch.setattr("luna.tts.sapi.SapiTTS", NoVoices)
    app = made(platform="win32", player=FakePlayer())
    assert app.speaker is not None
    assert app.speaker._tts.name == "edge+piper"  # Irina still stands by
    assert not any("needs the network" in w for w in app.warnings)


def test_edge_voice_on_non_windows_falls_back_to_piper(made):
    app = made(platform="linux", player=FakePlayer())
    assert app.speaker is not None
    assert app.speaker._tts.name == "edge+piper"


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


def test_piper_skipped_before_any_download_without_sherpa(made, monkeypatch):
    ensured = []
    monkeypatch.setattr("luna.tts.sapi.SapiTTS", _FakeSapi)
    monkeypatch.setattr(
        "luna.tts.piper_model.PiperModelStore.ensure", lambda *a, **k: ensured.append(1)
    )
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: None)
    app = made(platform="win32", player=FakePlayer())
    assert app.speaker._tts.name == "edge+sapi"
    assert len([w for w in app.warnings if "Piper" in w]) == 1
    assert ensured == []
