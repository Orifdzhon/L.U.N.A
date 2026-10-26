"""Wake mode wiring in VoiceInput / build_app: lifecycle, degrade paths, verifier sharing.

Fakes only: no vosk, no faster_whisper, no audio device, no network (see wake_support.py).
"""

import sys
import threading
import time
from pathlib import Path

from fakes import FakeHotkeyListener, FakeLiveMic
from luna.config.settings import Settings
from luna.core.errors import AudioError, ProviderUnavailable
from wake_support import FakeSpeaker, FakeStore, SpotterFactory, make, wait_for, wake_threads


def test_wake_off_by_default_opens_nothing(tmp_path, wake_apps):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    assert not app.voice_input.enabled and not app.voice_input.wake_active
    assert app.voice_input.wake_state == "OFF"
    assert "vosk" not in sys.modules and "faster_whisper" not in sys.modules
    assert wake_threads() == [] and mic.start_calls == 0


def test_enable_disable_lifecycle_and_idempotence(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    spotters = SpotterFactory()
    app = make(tmp_path, wake_apps, mic=mic, spotter_factory=spotters)
    voice = app.voice_input
    lines = []
    assert voice.enable_wake(lines.append) is None
    assert voice.enable_wake() is None  # idempotent
    assert voice.enabled and voice.wake_active and voice.wake_state == "ARMED"
    assert mic.started and mic.start_calls == 1 and len(wake_threads()) == 1
    assert len(spotters.made) == 1
    assert spotters.made[0][0] == Path("models") / "vosk-fake"
    assert voice._parts.frontend._live is voice._wake.listener

    voice.disable_wake()
    voice.disable_wake()  # idempotent
    assert voice.wake_state == "OFF" and not mic.started and wake_threads() == []
    assert voice._parts.frontend._live is None  # back to push-to-talk
    assert voice.enabled  # the hotkey stays

    assert voice.enable_wake() is None  # models are not rebuilt
    assert len(spotters.made) == 1 and len(fake_local.instances) == 1
    assert mic.started and len(wake_threads()) == 1


def test_status_line_shows_state_keyword_verify_and_model(tmp_path, wake_apps, fake_local):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    voice = app.voice_input
    assert voice.wake_status_line().startswith("Wake: OFF")
    assert voice.enable_wake() is None
    line = voice.wake_status_line()
    assert line.startswith("Wake: ARMED, keyword «луна», verify: local, model: ")
    assert "vosk-fake" in line


def test_model_store_failure_degrades_to_hotkey(tmp_path, wake_apps):
    mic = FakeLiveMic()
    store = FakeStore(ProviderUnavailable("no network"), present=False)
    lines = []
    app = make(tmp_path, wake_apps, mic=mic, wake_store=store)
    reason = app.voice_input.enable_wake(lines.append)
    assert reason == "cannot get the wake word model: no network"
    assert lines == ["Downloading the wake word model, 45 MB (one time)..."]
    assert app.voice_input.enabled and not app.voice_input.wake_active  # hotkey still on
    assert not mic.started and wake_threads() == []


def test_no_download_line_when_model_is_present(tmp_path, wake_apps, fake_local):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    lines = []
    assert app.voice_input.enable_wake(lines.append) is None
    assert not any("Downloading" in line for line in lines)
    assert any("verification model" in line for line in lines)


def test_spotter_failure_degrades(tmp_path, wake_apps, fake_local):
    spotters = SpotterFactory(ProviderUnavailable("the 'vosk' package is not usable"))
    app = make(tmp_path, wake_apps, mic=FakeLiveMic(), spotter_factory=spotters)
    reason = app.voice_input.enable_wake()
    assert reason is not None and "'vosk' package is not usable" in reason
    assert app.voice_input.enabled and not app.voice_input.wake_active


def test_engine_off_in_config(tmp_path, wake_apps):
    settings = Settings().with_api_keys({})
    settings.wakeword.engine = "off"
    app = make(tmp_path, wake_apps, settings=settings, mic=FakeLiveMic())
    assert app.voice_input.enable_wake() == "[wakeword] engine is off"


def test_mic_cannot_be_opened(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic(error=AudioError("microphone is busy"))
    app = make(tmp_path, wake_apps, mic=mic)
    reason = app.voice_input.enable_wake()
    assert reason == "cannot open the microphone (microphone is busy)"
    assert not app.voice_input.wake_active
    assert app.voice_input._parts.frontend._live is None
    assert wake_threads() == []


def test_verifier_reuses_the_chain_instance_and_loads_once(
    tmp_path, wake_apps, fake_local, monkeypatch
):
    monkeypatch.setattr("luna.voice.input.GroqWhisperSTT", None)  # groq is not used (no key)
    app = make(tmp_path, wake_apps, mic=FakeLiveMic(), stt=None)  # real chain assembly: local only
    voice = app.voice_input
    assert voice.enabled is False  # listen off: nothing built yet
    assert voice.enable_wake() is None
    (instance,) = fake_local.instances
    assert voice._wake.verifier._stt is instance
    assert voice._wake.verifier.name == "local"
    voice._warmup.join(2.0)  # the slice-3 warm-up thread ran in parallel
    assert instance.loads == 1


def test_verifier_built_from_stt_local_when_not_in_chain(tmp_path, wake_apps, fake_local):
    settings = Settings().with_api_keys({})
    settings.stt.local.model = "small"
    app = make(tmp_path, wake_apps, settings=settings, mic=FakeLiveMic())  # injected stt: no chain
    assert app.voice_input.enable_wake() is None
    (instance,) = fake_local.instances
    assert instance.args[0] == "small"
    assert app.voice_input._wake.verifier._stt is instance
    assert instance.loads == 1


def test_verifier_failure_falls_back_to_pass_through(tmp_path, wake_apps, fake_local, monkeypatch):
    monkeypatch.setattr(
        fake_local, "warm_up", lambda self: (_ for _ in ()).throw(ProviderUnavailable("no model"))
    )
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    voice = app.voice_input
    lines = []
    assert voice.enable_wake(lines.append) is None  # wake still starts
    assert voice._wake.verifier.name == "off"
    note = "Wake verification unavailable (no model); every detection starts a turn."
    assert note in lines and note in voice.notes
    assert "verify: off" in voice.wake_status_line()
    assert voice._parts.frontend._verifier is None  # no second stage


def test_verify_off_builds_no_local_model(tmp_path, wake_apps, fake_local):
    settings = Settings().with_api_keys({})
    settings.wakeword.verify = "off"
    app = make(tmp_path, wake_apps, settings=settings, mic=FakeLiveMic())
    assert app.voice_input.enable_wake() is None
    assert fake_local.instances == [] and "verify: off" in app.voice_input.wake_status_line()


def test_speaker_is_bound_and_unbound(tmp_path, wake_apps, fake_local):
    speaker = FakeSpeaker()
    app = make(tmp_path, wake_apps, mic=FakeLiveMic(), tts=None, player=None)
    voice = app.voice_input
    voice._speaker = speaker
    assert voice.enable_wake() is None
    assert len(speaker.listeners) == 1
    voice.disable_wake()
    assert speaker.listeners == []
    assert voice.enable_wake() is None and len(speaker.listeners) == 1  # rebound, not doubled


def test_listen_off_turns_wake_off(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    voice = app.voice_input
    assert voice.enable_wake() is None
    voice.disable()
    assert not voice.enabled and not voice.wake_active
    assert not mic.started and wake_threads() == []


def test_close_joins_threads_and_closes_the_microphone(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    assert app.voice_input.enable_wake() is None
    app.close()
    assert mic.closed and not mic.started
    assert wake_threads() == []
    assert app.voice_input._wake is None
    assert app.voice_input.enable_wake() == "the application is shutting down"


def test_mic_loss_returns_hotkey_to_the_microphone_and_retry_works(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    voice = app.voice_input
    assert voice.enable_wake() is None
    mic.fail(AudioError("microphone unplugged"))
    assert wait_for(lambda: voice.wake_state == "FAILED")
    assert wait_for(lambda: voice._parts.frontend._live is None)
    assert "microphone unplugged" in voice.wake_status_line()
    assert voice.enabled  # the hotkey stays

    assert voice.enable_wake() is None  # retry
    assert voice.wake_state == "ARMED" and voice._parts.frontend._live is not None
    assert len(wake_threads()) == 1


def test_measure_reads_through_a_tap_while_wake_is_on(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    voice = app.voice_input
    assert voice.enable_wake() is None
    stop = threading.Event()

    def feed():
        loud = bytes([0x00, 0x20]) * 480
        while not stop.is_set():
            mic.feed(loud)
            time.sleep(0.005)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    try:
        levels = voice.measure(0.09)
    finally:
        stop.set()
        feeder.join(1.0)
    assert levels.frames >= 1 and levels.peak > 0
    assert mic.start_calls == 1  # no second stream


def test_build_app_with_wake_enabled_starts_wake_mode(tmp_path, wake_apps, fake_local):
    lines = []
    app = make(tmp_path, wake_apps, wake=True, mic=FakeLiveMic(), wake_progress=lines.append)
    assert app.voice_input.wake_active
    text = "\n".join(app.startup_lines())
    assert "Wake: ARMED" in text and "microphone stays open" in text
    assert not [w for w in app.warnings if "Wake word disabled" in w]


def test_build_app_wake_failure_still_starts_with_hotkey(tmp_path, wake_apps):
    store = FakeStore(ProviderUnavailable("no network"), present=False)
    app = make(tmp_path, wake_apps, wake=True, mic=FakeLiveMic(), wake_store=store)
    (warning,) = [w for w in app.warnings if w.startswith("Wake word disabled")]
    assert warning == (
        "Wake word disabled: cannot get the wake word model: no network. "
        "Continuing with the hotkey."
    )
    assert app.voice_input.enabled and not app.voice_input.wake_active
    assert "Wake word disabled" in "\n".join(app.startup_lines())


def test_build_app_wake_without_voice_input_reports_only_the_voice_problem(tmp_path, wake_apps):
    from luna.core.errors import ToolError

    app = make(
        tmp_path, wake_apps, wake=True, mic=FakeLiveMic(), hotkey=FakeHotkeyListener(ToolError("x"))
    )
    assert [w for w in app.warnings if w.startswith("Voice input disabled")]
    assert not [w for w in app.warnings if w.startswith("Wake word disabled")]
    assert not app.voice_input.wake_active
