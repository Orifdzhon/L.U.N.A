"""REPL: /wake on|off|status, usage, the failure line. Fakes only (see test_app_wake)."""

import io
import time

import pytest

from fakes import FakeLiveMic
from luna.core.errors import AudioError, ProviderUnavailable
from luna.core.events import ProviderSwitched
from luna.ui.cli import HELP_TEXT, Repl
from luna.wakeword.listener import WakeState, WakeStateChanged
from wake_support import FakeStore, make, wait_for


class Script:
    def __init__(self, *items):
        self.items = list(items)

    def __call__(self, prompt):
        if not self.items:
            raise EOFError
        item = self.items.pop(0)
        return item() if callable(item) else item


def run(app, *lines):
    out = io.StringIO()
    assert Repl(app, Script(*lines), out).run() == 0
    return out.getvalue()


def test_help_mentions_wake():
    assert "/wake [on|off]" in HELP_TEXT


def test_wake_status_when_off(tmp_path, wake_apps):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    text = run(app, "/wake")
    assert "Wake: OFF" in text and "«луна»" in text


def test_wake_on_then_status_then_off(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    text = run(app, "/wake on", "/wake", "/wake off", "/wake")
    assert "🌙 listening for «Луна»" in text
    assert "Wake: ARMED, keyword «луна», verify: local" in text
    assert "microphone stays open" in text
    assert "Wake word off: the microphone is closed" in text
    assert text.rstrip().splitlines()[-1].startswith("Wake: OFF") or "Wake: OFF" in text
    assert not mic.started and not app.voice_input.wake_active


def test_wake_on_failure_is_explained(tmp_path, wake_apps):
    store = FakeStore(ProviderUnavailable("no network"), present=False)
    app = make(tmp_path, wake_apps, mic=FakeLiveMic(), wake_store=store)
    text = run(app, "/wake on")
    assert "Downloading the wake word model, 45 MB (one time)..." in text
    assert "Wake word disabled: cannot get the wake word model: no network." in text
    assert "Continuing with the hotkey." in text


@pytest.mark.parametrize("line", ["/wake maybe", "/wake on off", "/wake ON now"])
def test_wake_usage(tmp_path, wake_apps, line):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    assert "Usage: /wake [on|off]" in run(app, line)
    assert not app.voice_input.wake_active


def test_wake_command_is_case_insensitive(tmp_path, wake_apps, fake_local):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    run(app, "/wake ON")
    assert app.voice_input.wake_active


def test_push_to_talk_unchanged_when_wake_is_off(tmp_path, wake_apps):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    text = run(app, "/listen on", "/wake")
    assert "Listen: on" in text and "Wake: OFF" in text
    assert app.voice_input.enabled and not mic.started and mic.start_calls == 0
    assert app.voice_input._parts.frontend._live is None


def test_mic_failure_prints_one_wake_failed_line(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    assert app.voice_input.enable_wake() is None

    def fail_mic():
        mic.fail(AudioError("microphone unplugged"))
        assert wait_for(lambda: app.voice_input.wake_state == "FAILED")
        time.sleep(0.4)  # the status thread polls every 100 ms
        return "exit"

    text = run(app, fail_mic)
    assert text.count("wake failed: microphone error: microphone unplugged") == 1
    assert "Hotkey works" in text


def test_ordinary_wake_state_changes_are_not_printed(tmp_path, wake_apps):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())

    def publish():
        for state in (WakeState.ARMED, WakeState.HELD, WakeState.OFF):
            app.bus.publish(WakeStateChanged(state, "speaking"))
        app.bus.publish(ProviderSwitched("a", "b", "x"))
        time.sleep(0.3)
        return "exit"

    assert "wake" not in run(app, publish).lower().replace("l.u.n.a. text mode", "")
