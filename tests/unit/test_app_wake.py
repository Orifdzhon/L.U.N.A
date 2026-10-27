"""Wake mode wiring in VoiceInput / build_app: lifecycle, degrade paths, verifier sharing.

Fakes only: no vosk, no faster_whisper, no audio device, no network (see wake_support.py).
"""

import logging
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
    assert voice_wake_unbuilt(app)
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


def test_status_line_shows_the_conversation_countdown(tmp_path, wake_apps, fake_local):
    settings = Settings().with_api_keys({})
    settings.wakeword.follow_up_s = 90.0
    app = make(tmp_path, wake_apps, settings=settings, mic=FakeLiveMic())
    voice = app.voice_input
    assert voice.enable_wake() is None
    assert "conversation: on (90 s after a reply)" in voice.wake_status_line()
    voice._wake.listener.open_window(63.0)
    assert "conversation: 63 s left" in voice.wake_status_line()


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
    settings.wakeword.verify = "local"
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


def test_build_app_hotkey_taken_keeps_wake_mode(tmp_path, wake_apps, fake_local, caplog):
    from luna.core.errors import ToolError

    caplog.set_level(logging.WARNING, logger="luna.voice.frontend")
    app = make(
        tmp_path,
        wake_apps,
        wake=True,
        mic=FakeLiveMic(),
        hotkey=FakeHotkeyListener(ToolError("hotkey is taken")),
    )
    assert app.voice_input.enabled and app.voice_input.wake_active
    assert not [w for w in app.warnings if w.startswith("Voice input disabled")]
    assert app.voice_input.status_line() == "Listen: on (no hotkey, wake word only)"
    assert any("not available" in n for n in app.voice_input.notes)
    assert sum("works without it" in r.getMessage() for r in caplog.records) == 1


def test_listen_without_wake_still_fails_when_the_hotkey_is_taken(tmp_path, wake_apps):
    from luna.core.errors import ToolError

    app = make(tmp_path, wake_apps, mic=FakeLiveMic(), hotkey=FakeHotkeyListener(ToolError("x")))
    assert app.voice_input.enable() == "x"
    assert not app.voice_input.enabled


def test_listen_after_wake_without_hotkey_reports_the_missing_hotkey(
    tmp_path, wake_apps, fake_local
):
    from luna.core.errors import ToolError

    app = make(tmp_path, wake_apps, mic=FakeLiveMic(), hotkey=FakeHotkeyListener(ToolError("x")))
    assert app.voice_input.enable_wake() is None
    assert "not available" in (app.voice_input.enable() or "")
    assert app.voice_input.wake_active  # wake is unaffected


def voice_wake_unbuilt(app):
    voice = app.voice_input
    return voice._wake is None and voice._wake_local is None and voice._parts is None


class BlockingStore(FakeStore):
    """``ensure()`` waits until the test releases it (a slow download)."""

    def __init__(self):
        super().__init__(present=False)
        self.entered = threading.Event()
        self.release = threading.Event()

    def ensure(self):
        self.entered.set()
        assert self.release.wait(5.0)
        return super().ensure()


def test_slow_download_does_not_block_close_and_result_is_discarded(
    tmp_path, wake_apps, fake_local
):
    store = BlockingStore()
    spotters = SpotterFactory()
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic, wake_store=store, spotter_factory=spotters)
    voice = app.voice_input
    result = []
    worker = threading.Thread(target=lambda: result.append(voice.enable_wake()), daemon=True)
    worker.start()
    assert store.entered.wait(3.0)

    started = time.monotonic()
    app.close()  # must not wait for the download
    assert time.monotonic() - started < 2.0

    store.release.set()
    worker.join(3.0)
    assert result == ["the application is shutting down"]
    assert voice._wake is None and wake_threads() == []
    assert not mic.started
    # the spotter, if it was built before the check, is closed again
    assert all(spotter.closed for _, spotter in spotters.made)


def test_second_enable_wake_while_building_is_refused(tmp_path, wake_apps, fake_local):
    store = BlockingStore()
    app = make(tmp_path, wake_apps, mic=FakeLiveMic(), wake_store=store)
    voice = app.voice_input
    worker = threading.Thread(target=voice.enable_wake, daemon=True)
    worker.start()
    assert store.entered.wait(3.0)
    assert voice.enable_wake() == "wake mode is already starting"
    store.release.set()
    worker.join(5.0)
    assert voice.wake_active


def test_pass_through_fallback_is_visible_in_the_status_line(
    tmp_path, wake_apps, fake_local, monkeypatch
):
    def broken(self):
        raise ProviderUnavailable("no model")

    monkeypatch.setattr(fake_local, "warm_up", broken)
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    assert app.voice_input.enable_wake() is None
    assert "verification off — false wakes may be sent to cloud STT" in (
        app.voice_input.wake_status_line()
    )


def test_late_failure_report_of_a_restarted_listener_is_ignored(tmp_path, wake_apps, fake_local):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    voice = app.voice_input
    assert voice.enable_wake() is None
    voice._on_wake_failed("stale report")  # the listener is ARMED again
    assert voice._parts.frontend._live is voice._wake.listener
    assert voice._wake_failure is None


def test_measure_holds_the_listener_and_releases_it(tmp_path, wake_apps, fake_local):
    mic = FakeLiveMic()
    app = make(tmp_path, wake_apps, mic=mic)
    voice = app.voice_input
    assert voice.enable_wake() is None
    listener = voice._wake.listener
    calls = []
    original = listener.set_hold
    listener.set_hold = lambda reason, active: (
        calls.append((reason, active)),
        original(reason, active),
    )
    stop = threading.Event()

    def feed():
        while not stop.is_set():
            mic.feed(bytes([0x00, 0x20]) * 480)
            stop.wait(0.005)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    try:
        voice.measure(0.09)
    finally:
        stop.set()
        feeder.join(1.0)
    assert calls == [("diag", True), ("diag", False)]
    assert "diag" not in listener._gate.holds


def test_listener_close_error_does_not_break_close(tmp_path, wake_apps, fake_local):
    app = make(tmp_path, wake_apps, mic=FakeLiveMic())
    voice = app.voice_input
    assert voice.enable_wake() is None
    listener = voice._wake.listener
    real_close = listener.close

    def boom():
        real_close()
        raise RuntimeError("boom")

    listener.close = boom
    app.close()  # no exception
    assert voice._wake is None
