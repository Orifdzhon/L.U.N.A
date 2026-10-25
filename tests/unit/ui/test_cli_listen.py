"""Voice input wiring: build_app + Repl with fake hotkey, microphone, STT and TTS."""

import array
import io
import threading
import time

import pytest

from fakes import (
    FakeAppCatalog,
    FakeBrowser,
    FakeHotkeyListener,
    FakeLauncher,
    FakeLLM,
    FakeMic,
    FakePlayer,
    FakeSearch,
    FakeSTT,
    FakeTTS,
)
from luna.app import build_app
from luna.config.settings import Settings
from luna.core.errors import AudioError, ToolError
from luna.core.models import Reply
from luna.llm.base import ChatResponse
from luna.platform.base import AppEntry
from luna.stt.base import Transcript
from luna.ui.cli import Repl
from luna.voice.input import MicLevels, suggest_threshold

FRAME_BYTES = 960  # 30 ms of 16 kHz int16


def frame(amplitude: int) -> bytes:
    return array.array("h", [amplitude, -amplitude] * (FRAME_BYTES // 4)).tobytes()


def phrase() -> list[bytes]:
    """Quiet warm-up, 12 speech frames, then enough silence to end the phrase."""
    return [frame(2)] * 10 + [frame(3000)] * 12 + [frame(2)] * 40


def say(text: str) -> ChatResponse:
    return ChatResponse(text, (), "fake", "m", "stop")


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Script:
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
        return self.items.pop(0)


class Rig:
    def __init__(self, tmp_path, *, listen=True, speak=True, frames=None, llm=None, **kw):
        settings = Settings().with_api_keys({})
        settings.voice.listen = listen
        settings.tts.enabled = speak
        self.tts, self.player = FakeTTS(), FakePlayer()
        self.hotkey = kw.pop("hotkey", FakeHotkeyListener())
        self.mic = kw.pop("mic", FakeMic(frames if frames is not None else phrase()))
        self.stt = kw.pop(
            "stt", FakeSTT([Transcript("расскажи что-нибудь", "ru", provider="fake")])
        )
        self.player.block = kw.pop("block", False)
        catalog = FakeAppCatalog([AppEntry("Telegram", "Telegram.TelegramDesktop")])
        extra = {"tts": self.tts, "player": self.player} if speak else {}
        self.app = build_app(
            settings,
            llm=llm or FakeLLM([say("Привет.")]),
            browser=FakeBrowser(),
            catalog=catalog,
            launcher=FakeLauncher(catalog),
            search=FakeSearch(),
            log_dir=tmp_path,
            stt=self.stt,
            mic=self.mic,
            hotkey=self.hotkey,
            **extra,
            **kw,
        )
        self.out = io.StringIO()

    def run(self, script):
        code = Repl(self.app, script, self.out).run()
        return code, self.out.getvalue()


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(**kw):
        r = Rig(tmp_path, **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.app.close()


def test_startup_lines_listen_on(rig):
    r = rig()
    assert r.hotkey.running and r.hotkey.hotkey == "ctrl+alt+space"
    lines = r.app.startup_lines()
    assert any(line.startswith("Listen: on (Ctrl+Alt+Space)") for line in lines)


def test_listen_off_by_default_touches_nothing(rig):
    r = rig(listen=False)
    assert not r.hotkey.running and r.mic.start_calls == 0
    assert any(line.startswith("Listen: off") for line in r.app.startup_lines())


def test_voice_turn_is_printed_and_spoken(rig):
    r = rig()
    hooks = {0: lambda: (r.hotkey.press(), wait_for(lambda: len(r.tts.calls) >= 1))}
    code, out = r.run(Script("exit", hooks=hooks))
    assert code == 0
    assert "(listening...)" in out
    assert "(heard) расскажи что-нибудь" in out
    assert "Привет." in out
    assert r.tts.calls and r.tts.calls[0].lang == "ru"


def test_voice_turn_without_speaker_is_only_printed(rig):
    r = rig(speak=False)
    hooks = {0: lambda: (r.hotkey.press(), wait_for(lambda: "Привет." in r.out.getvalue()))}
    _, out = r.run(Script("exit", hooks=hooks))
    assert "Привет." in out


def test_stale_reply_is_printed_not_spoken(rig):
    r = rig()
    repl = Repl(r.app, Script(), r.out)
    repl._voice_stale_reply(Reply("Открыл ютуб.", "ru"))
    time.sleep(0.2)
    assert "Открыл ютуб." in r.out.getvalue()
    assert r.tts.calls == []


def test_typed_input_stops_voice_speech(rig):
    r = rig(block=True)
    hooks = {
        0: lambda: (r.hotkey.press(), r.player.started.wait(3.0)),
    }
    r.run(Script("/providers", "exit", hooks=hooks))
    assert wait_for(lambda: r.player.stop_count >= 1)


def test_listen_toggle(rig):
    r = rig(listen=False)
    states = []
    hooks = {
        1: lambda: states.append(r.hotkey.running),
        2: lambda: states.append(r.hotkey.running),
    }
    _, out = r.run(Script("/listen", "/listen", "exit", hooks=hooks))
    assert states == [True, False]
    assert "Listen: on (Ctrl+Alt+Space)" in out
    assert "Listen: off" in out


def test_listen_on_off_arguments_and_usage(rig):
    r = rig(listen=False)
    _, out = r.run(Script("/listen on", "/listen on", "/listen off", "/listen maybe", "exit"))
    assert out.count("Listen: on") == 2 and "Listen: off" in out
    assert "Usage: /listen [on|off]" in out


def test_listen_reports_hotkey_conflict(rig):
    r = rig(
        listen=False, hotkey=FakeHotkeyListener(ToolError("hotkey is taken: see [voice] hotkey"))
    )
    _, out = r.run(Script("/listen", "exit"))
    assert "Voice input disabled: hotkey is taken" in out and "text mode" in out


def test_help_mentions_listening(rig):
    _, out = rig(listen=False).run(Script("/help", "exit"))
    assert "/listen" in out and "/mic" in out


def test_mic_diagnostic_suggests_threshold(rig):
    frames = [frame(3)] * 20 + [frame(300)] * 30 + [frame(500)] * 50  # peak ~0.015
    r = rig(listen=False, frames=frames)
    _, out = r.run(Script("/mic", "exit"))
    assert "Noise floor: 0.0001" in out
    assert "peak: 0.0153" in out
    assert "Suggested [voice] vad_energy_threshold = 0.0031" in out
    assert r.mic.stop_calls >= 1 and not r.mic.started


def test_mic_diagnostic_without_speech(rig):
    r = rig(listen=False, frames=[frame(3)] * 100)
    _, out = r.run(Script("/mic", "exit"))
    assert "No speech heard" in out


def test_mic_diagnostic_failure(rig):
    r = rig(listen=False, mic=FakeMic(error=AudioError("device busy")))
    _, out = r.run(Script("/mic", "exit"))
    assert "Microphone check failed" in out and "device busy" in out


def test_mic_refused_while_a_turn_runs(rig):
    r = rig(frames=[])  # the recording waits for audio
    r.hotkey.press()
    assert wait_for(lambda: r.mic.started)
    _, out = r.run(Script("/mic", "exit"))
    assert "Microphone check failed: a voice request is running" in out


def test_shutdown_leaves_no_threads(rig):
    r = rig()
    hooks = {0: lambda: (r.hotkey.press(), wait_for(lambda: len(r.tts.calls) >= 1))}
    r.run(Script("exit", hooks=hooks))
    r.app.close()
    assert not r.hotkey.running and r.mic.closed
    assert wait_for(
        lambda: not [t for t in threading.enumerate() if t.name in ("luna-voice", "luna-ui-status")]
    )


def test_close_while_listening_cancels_the_turn(rig):
    r = rig(frames=[])
    r.hotkey.press()
    assert wait_for(lambda: r.mic.started)
    r.app.close()
    assert wait_for(lambda: not [t for t in threading.enumerate() if t.name == "luna-voice"])
    assert not r.hotkey.running


def test_suggest_threshold_with_owner_numbers():
    # owner's webcam microphone: floor 0.0001, speech peak 0.016
    assert suggest_threshold(0.0001, 0.016) == 0.0032
    assert suggest_threshold(0.0001, 0.0003) is None
    assert suggest_threshold(0.01, 0.05) == 0.04  # noisy room: four times the floor
    assert isinstance(MicLevels(0.0, 0.0, 1, None), MicLevels)
