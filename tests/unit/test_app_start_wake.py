"""build_app(start_wake=False): the wake model is not enabled inside build_app."""

from wake_support import make, wake_threads


def test_start_wake_false_does_not_touch_wake(tmp_path, wake_apps, fake_local):
    app = make(tmp_path, wake_apps, wake=True, start_wake=False)
    voice = app.voice_input
    assert voice.enabled  # the hotkey side is still up
    assert voice.wake_state == "OFF" and not voice.wake_active
    assert wake_threads() == []
    assert not any("Wake word disabled" in w for w in app.warnings)


def test_start_wake_default_enables_wake(tmp_path, wake_apps, fake_local):
    app = make(tmp_path, wake_apps, wake=True)
    assert app.voice_input.wake_active
