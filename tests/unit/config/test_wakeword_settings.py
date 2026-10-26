import pytest
from pydantic import ValidationError

from luna.config.loader import load_settings
from luna.config.settings import Settings, WakeWordSettings

LEGACY = '[wakeword]\nengine = "vosk"\nkeywords = ["луна"]\nhotkey = "ctrl+alt+space"\n'


def _load(tmp_path, text):
    path = tmp_path / "luna.toml"
    path.write_text(text, encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    return load_settings(path, env={}, cwd=cwd)


def test_defaults_are_safe_off():
    w = Settings().wakeword
    assert w.enabled is False
    assert (w.engine, w.keywords, w.model) == ("vosk", ["луна"], "small-ru")
    assert (w.cooldown_s, w.speaking_tail_ms, w.command_timeout_s) == (2.0, 400, 5.0)
    assert w.mute_while_speaking is True
    assert (w.cue, w.follow_up_s, w.model_dir) == ("off", 0.0, None)


def test_legacy_block_loads_with_exactly_one_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        settings = _load(tmp_path, LEGACY)
    assert settings.wakeword == WakeWordSettings()
    warnings = [r for r in caplog.records if "hotkey" in r.getMessage()]
    assert len(warnings) == 1
    assert "[voice] hotkey" in warnings[0].getMessage()


def test_keywords_are_normalised():
    assert WakeWordSettings(keywords=[" Луна ", "LUNA"]).keywords == ["луна", "luna"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("threshold", 1.5),
        ("threshold", -0.1),
        ("keywords", ["  "]),
        ("keywords", []),
        ("engine", "porcupine"),
        ("engine", "onnx"),
        ("cooldown_s", -1),
        ("follow_up_s", 301),
        ("speaking_tail_ms", -1),
        ("cue", "loud"),
        ("bogus", 1),
    ],
)
def test_invalid_values_name_the_field(field, value):
    with pytest.raises(ValidationError) as exc:
        WakeWordSettings(**{field: value})
    assert field in str(exc.value)


def test_verify_defaults_and_override(tmp_path):
    w = Settings().wakeword
    assert (w.verify, w.verify_words) == (
        "local",
        ["луна", "луну", "луне", "луны", "luna"],
    )
    assert w.log_rejects is False
    off = _load(tmp_path, '[wakeword]\nverify = "off"\nverify_words = [" Луна "]\n').wakeword
    assert (off.verify, off.verify_words) == ("off", ["луна"])


def test_verify_cloud_and_blank_words_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        WakeWordSettings(verify="cloud")
    with pytest.raises(ValidationError):
        WakeWordSettings(verify_words=[])
    with pytest.raises(ValidationError):
        WakeWordSettings(verify_words=[" "])


def test_log_rejects_is_opt_in_and_owner_config_loads_unchanged(tmp_path):
    assert _load(tmp_path, "[wakeword]\nlog_rejects = true\n").wakeword.log_rejects is True
    old = _load(tmp_path, '[wakeword]\nenabled = true\nverify_words = ["луна", "luna"]\n')
    assert old.wakeword.log_rejects is False
    assert old.wakeword.verify_words == ["луна", "luna"]
