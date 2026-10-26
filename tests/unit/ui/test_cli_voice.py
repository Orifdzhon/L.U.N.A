import io
import threading
import time

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
from luna.core.models import Reply
from luna.core.prompts import fallback_text
from luna.llm.base import ChatResponse
from luna.platform.base import AppEntry
from luna.ui.cli import Repl


def say(text: str) -> ChatResponse:
    return ChatResponse(text, (), "fake", "m", "stop")


class Script:
    """input() replacement; ``hooks[i]`` runs before the i-th prompt returns."""

    def __init__(self, *items, hooks=None):
        self.items = list(items)
        self.hooks = hooks or {}
        self.n = 0

    def __call__(self, prompt):
        i = self.n
        self.n += 1
        if i in self.hooks:
            self.hooks[i]()
        if not self.items:
            raise EOFError
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def build(tmp_path, *, tts=None, player=None, llm=None, enabled=True):
    settings = Settings().with_api_keys({})
    settings.tts.enabled = enabled
    catalog = FakeAppCatalog([AppEntry("Telegram", "Telegram.TelegramDesktop")])
    extra = {"tts": tts, "player": player} if enabled else {}
    return build_app(
        settings,
        llm=llm or FakeLLM([]),
        browser=FakeBrowser(),
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        search=FakeSearch(),
        log_dir=tmp_path,
        **extra,
    )


@pytest.fixture
def voice(tmp_path):
    tts, player = FakeTTS(), FakePlayer()
    apps = []

    def make(llm=None, block=False):
        player.block = block
        app = build(tmp_path, tts=tts, player=player, llm=llm)
        apps.append(app)
        return app

    yield make, tts, player
    for app in apps:
        app.close()


def run(app, script):
    out = io.StringIO()
    code = Repl(app, script, out).run()
    return code, out.getvalue()


def test_replies_are_spoken_with_their_language(voice):
    make, tts, _ = voice
    app = make()
    hooks = {
        1: lambda: wait_for(lambda: len(tts.calls) >= 1),
        2: lambda: wait_for(lambda: len(tts.calls) >= 2),
    }
    run(app, Script("открой ютуб", "what time is it", "exit", hooks=hooks))
    assert wait_for(lambda: len(tts.calls) >= 2)
    assert tts.calls[0].lang == "ru"
    assert tts.calls[1].lang == "en"


def test_cancelled_and_timeout_fallbacks_are_not_spoken(voice):
    make, tts, _ = voice
    repl = Repl(make(), Script(), io.StringIO())
    for kind in ("cancelled", "timeout"):
        for lang in ("ru", "en"):
            repl._speak(Reply(fallback_text(kind, lang), lang))
    time.sleep(0.2)
    assert tts.calls == []
    repl._speak(Reply("Привет.", "ru"))
    assert wait_for(lambda: len(tts.calls) == 1)


def test_mute_toggle(voice):
    make, tts, _ = voice
    hooks = {4: lambda: wait_for(lambda: len(tts.calls) >= 1)}
    script = Script("/mute", "открой ютуб", "/mute", "открой ютуб", "exit", hooks=hooks)
    code, out = run(make(), script)
    assert code == 0
    assert "muted" in out
    assert "unmuted" in out
    assert wait_for(lambda: len(tts.calls) >= 1)
    time.sleep(0.2)
    assert len(tts.calls) == 1  # only after the second /mute


def test_stop_command_cuts_playback(voice):
    make, _, player = voice
    app = make(block=True)
    started = {}
    script = Script(
        "открой ютуб",
        "/stop",
        "exit",
        hooks={1: lambda: started.setdefault("ok", player.started.wait(2.0))},
    )
    run(app, script)
    assert started["ok"]
    assert wait_for(lambda: player.returned >= 1)


def test_new_request_stops_previous_speech(voice):
    make, _, player = voice
    app = make(FakeLLM([say("Second.")]), block=True)
    script = Script(
        "открой ютуб", "what is it", "exit", hooks={1: lambda: player.started.wait(2.0)}
    )
    run(app, script)
    assert wait_for(lambda: player.stop_count >= 1)
    assert wait_for(lambda: player.returned >= 1)


def test_ctrl_c_during_speech_stops_it_without_quitting(voice):
    make, _, player = voice
    app = make(block=True)
    script = Script(
        "открой ютуб",
        KeyboardInterrupt(),
        "/providers",
        "exit",
        hooks={1: lambda: player.started.wait(2.0)},
    )
    code, out = run(app, script)
    assert code == 0
    assert "speech stopped" in out
    assert "Active LLM providers" in out  # the REPL kept running
    assert "press Ctrl+C again" not in out
    assert wait_for(lambda: player.returned >= 1)


def test_ctrl_c_without_speech_keeps_old_behaviour(voice):
    make, *_ = voice
    code, out = run(make(), Script(KeyboardInterrupt(), KeyboardInterrupt(), "never"))
    assert code == 0
    assert "press Ctrl+C again" in out


def test_voice_commands_without_speaker(tmp_path):
    app = build(tmp_path, enabled=False)
    try:
        code, out = run(app, Script("/mute", "/stop", "/voice", "exit"))
    finally:
        app.close()
    assert code == 0
    assert out.count("Voice is off") == 2
    assert "Nothing is being spoken" in out


def test_voice_command_shows_piper_provider(voice):
    make, *_ = voice
    _, out = run(make(), Script("/voice", "exit"))
    assert "piper ru=ru_RU-irina-medium, en=en_US-kristin-medium (fallback: sapi Irina/Zira)" in out


def test_voice_command_shows_settings(voice):
    make, *_ = voice
    app = make()
    app.settings.tts.provider = "edge"
    _, out = run(app, Script("/voice", "exit"))
    assert "Irina" in out
    assert "Zira" in out
    assert "edge ru=ru-RU-SvetlanaNeural, en=en-US-JennyNeural (fallback: sapi Irina/Zira)" in out


def test_voice_command_shows_sapi_provider(voice):
    make, *_ = voice
    app = make()
    app.settings.tts.provider = "sapi"
    _, out = run(app, Script("/voice", "exit"))
    assert "sapi ru=Irina, en=Zira" in out
    assert "edge" not in out.replace("Voice", "")


def test_close_leaves_no_speaker_threads(voice):
    make, *_ = voice
    app = make()
    run(app, Script("открой ютуб", "exit"))
    app.close()
    assert wait_for(
        lambda: not any(t.name.startswith("luna-speaker") for t in threading.enumerate())
    )
