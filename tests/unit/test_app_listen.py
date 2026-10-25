"""build_app voice input: degraded start, STT chain assembly, warm-up."""

from typing import ClassVar

import pytest

from fakes import (
    FakeAppCatalog,
    FakeBrowser,
    FakeHotkeyListener,
    FakeLauncher,
    FakeLLM,
    FakeMic,
    FakeSearch,
    FakeSTT,
)
from luna.app import build_app
from luna.config.settings import Settings
from luna.core.errors import ToolError
from luna.platform.base import AppEntry
from luna.stt.chain import STTChain


def build(tmp_path, settings=None, **kw):
    settings = settings or Settings().with_api_keys({})
    settings.voice.listen = kw.pop("listen", True)
    catalog = FakeAppCatalog([AppEntry("Telegram", "Telegram.TelegramDesktop")])
    return build_app(
        settings,
        llm=FakeLLM([]),
        browser=FakeBrowser(),
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        search=FakeSearch(),
        log_dir=tmp_path,
        **kw,
    )


@pytest.fixture
def apps():
    made = []
    yield made
    for app in made:
        app.close()


def degraded(app):
    return [w for w in app.warnings if w.startswith("Voice input disabled")]


def test_hotkey_taken_degrades_with_hint(tmp_path, apps):
    taken = ToolError("hotkey 'ctrl+alt+space' is already taken; choose another in [voice] hotkey")
    app = build(tmp_path, stt=FakeSTT([]), mic=FakeMic(), hotkey=FakeHotkeyListener(taken))
    apps.append(app)
    (line,) = degraded(app)
    assert "already taken" in line and "[voice] hotkey" in line
    assert line.endswith("Continuing in text mode.")
    assert not app.voice_input.enabled
    assert "Listen:" not in "\n".join(app.startup_lines())  # one clear line, no second one


def test_non_windows_has_no_hotkey(tmp_path, apps):
    app = build(tmp_path, platform="linux", stt=FakeSTT([]), mic=FakeMic())
    apps.append(app)
    assert "only supported on Windows" in degraded(app)[0]


def test_no_microphone(tmp_path, apps, monkeypatch):
    monkeypatch.setattr("luna.voice.input.check_audio_input", lambda: "no audio input device")
    app = build(tmp_path, stt=FakeSTT([]), hotkey=FakeHotkeyListener())
    apps.append(app)
    assert "no audio input device" in degraded(app)[0]


def test_no_stt_engine(tmp_path, apps):
    settings = Settings().with_api_keys({})
    settings.stt.chain = ["groq"]  # no key
    hotkey = FakeHotkeyListener()
    app = build(tmp_path, settings, mic=FakeMic(), hotkey=hotkey)
    apps.append(app)
    assert "no speech recognition engine" in degraded(app)[0]
    assert not hotkey.running  # nothing half-started


def test_degraded_start_still_works_in_text_mode(tmp_path, apps):
    app = build(tmp_path, stt=FakeSTT([]), mic=FakeMic(), hotkey=FakeHotkeyListener(ToolError("x")))
    apps.append(app)
    assert app.orchestrator is not None and app.voice_input.enable() == "x"


class _FakeLocal:
    name = "local"
    instances: ClassVar[list] = []

    def __init__(self, *args):
        self.args = args
        self.warmups = 0
        self.joined = None
        _FakeLocal.instances.append(self)

    def start_warm_up(self):
        import threading

        self.warmups += 1
        thread = threading.Thread(target=lambda: None)
        thread.start()
        return thread


class _FakeGroq:
    name = "groq"

    def __init__(self, key, model):
        self.model = model
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_engines(monkeypatch):
    _FakeLocal.instances = []
    monkeypatch.setattr("luna.voice.input.FasterWhisperSTT", _FakeLocal)
    monkeypatch.setattr("luna.voice.input.GroqWhisperSTT", _FakeGroq)


def test_chain_without_groq_key_uses_local_and_warms_it_up(tmp_path, apps, fake_engines):
    app = build(tmp_path, mic=FakeMic(), hotkey=FakeHotkeyListener())
    apps.append(app)
    assert app.voice_input.enabled
    (local,) = _FakeLocal.instances
    assert local.args[0] == "base"  # default local model
    assert local.warmups == 1
    notes = " ".join(app.voice_input.notes)
    assert (
        "groq skipped (GROQ_API_KEY is not set)" in notes and "Speech recognition: local." in notes
    )


def test_chain_with_groq_key_keeps_chain_order(tmp_path, apps, fake_engines):
    settings = Settings().with_api_keys({"GROQ_API_KEY": "k"})
    app = build(tmp_path, settings, mic=FakeMic(), hotkey=FakeHotkeyListener())
    apps.append(app)
    parts = app.voice_input._parts
    assert isinstance(parts.stt, STTChain)
    assert [p.name for p in parts.stt._providers] == ["groq", "local"]
    assert "Speech recognition: groq -> local." in app.voice_input.notes
    groq = parts.closers[-1]
    app.close()
    assert isinstance(groq, _FakeGroq) and groq.closed


def test_no_warm_up_when_listen_is_off(tmp_path, apps, fake_engines):
    app = build(tmp_path, listen=False, mic=FakeMic(), hotkey=FakeHotkeyListener())
    apps.append(app)
    assert _FakeLocal.instances == []
    assert app.voice_input.enable() is None  # /listen on later builds and warms up
    assert [x.warmups for x in _FakeLocal.instances] == [1]
    app.voice_input.disable()
    assert app.voice_input.enable() is None
    assert [x.warmups for x in _FakeLocal.instances] == [1]  # not loaded twice


def test_local_alone_is_not_warmed_up_when_chain_has_no_local(tmp_path, apps, fake_engines):
    settings = Settings().with_api_keys({"GROQ_API_KEY": "k"})
    settings.stt.chain = ["groq"]
    app = build(tmp_path, settings, mic=FakeMic(), hotkey=FakeHotkeyListener())
    apps.append(app)
    assert app.voice_input.enabled and _FakeLocal.instances == []
