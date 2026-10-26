import pytest
from pydantic import ValidationError

from luna.config.loader import load_settings
from luna.config.settings import Settings, STTSettings, VoiceSettings
from luna.core.errors import ConfigError


def _load(tmp_path, text):
    cfg = tmp_path / "luna.toml"
    cfg.write_text(text, encoding="utf-8")
    return load_settings(cfg, env={}, cwd=tmp_path)


def test_defaults_are_typed_models():
    s = Settings()
    assert isinstance(s.stt, STTSettings)
    assert isinstance(s.voice, VoiceSettings)
    assert s.stt.chain == ["groq", "local"]
    assert s.stt.language_hints == ["ru", "en"]
    assert s.stt.timeout_s == 5.0
    assert (s.stt.groq.model, s.stt.groq.api_key_env) == ("whisper-large-v3-turbo", "GROQ_API_KEY")
    assert (s.stt.local.model, s.stt.local.device) == ("base", "cpu")
    assert (s.stt.local.compute_type, s.stt.local.beam_size) == ("int8", 1)
    assert s.stt.local.model_dir is None
    v = s.voice
    assert v.listen is False and v.start_beep is False
    assert v.hotkey == "ctrl+alt+space"
    assert v.vad_energy_threshold == 0.003  # owner-calibrated (Logi C270 speech 0.005-0.016)
    assert (v.silence_ms, v.preroll_ms, v.max_utterance_s, v.frame_ms) == (700, 300, 10, 30)
    assert v.input_device is None


def test_empty_config_loads(tmp_path):
    assert _load(tmp_path, "").stt == Settings().stt


@pytest.mark.parametrize("chain", ["[]", '["groq", "groq"]', '["whisper"]'])
def test_bad_chain_rejected(tmp_path, chain):
    with pytest.raises(ConfigError, match=r"stt\.chain"):
        _load(tmp_path, f"[stt]\nchain = {chain}\n")


def test_chain_order_is_kept(tmp_path):
    assert _load(tmp_path, '[stt]\nchain = ["local", "groq"]\n').stt.chain == ["local", "groq"]


@pytest.mark.parametrize(
    "hotkey", ["space", "ctrl+alt", "Ctrl+Alt+Space", "ctrl+a+b", "ctrl+ctrl+a", ""]
)
def test_bad_hotkey_rejected(hotkey):
    with pytest.raises(ValidationError):
        VoiceSettings(hotkey=hotkey)


@pytest.mark.parametrize("hotkey", ["ctrl+alt+space", "win+shift+f9", "alt+a"])
def test_good_hotkey_accepted(hotkey):
    assert VoiceSettings(hotkey=hotkey).hotkey == hotkey


@pytest.mark.parametrize(
    ("section", "key"),
    [("stt", "timeout_s"), ("voice", "silence_ms"), ("voice", "max_utterance_s")],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_values_rejected(tmp_path, section, key, value):
    with pytest.raises(ConfigError, match=f"{section}.{key}"):
        _load(tmp_path, f"[{section}]\n{key} = {value}\n")


@pytest.mark.parametrize(
    ("section", "text"),
    [
        ("voice", "frame_ms = 25"),
        ("voice", "max_utterance_s = 61"),
        ("voice", "min_speech_ms = 20000"),
        ("voice", "input_device = -1"),
        ("voice", 'input_device = " "'),
        ("voice", "preroll_ms = -1"),
        ("voice", "vad_noise_margin = 0.5"),
        ("voice", "bogus = 1"),
        ("stt", 'prompt = "' + "x" * 501 + '"'),
        ("stt", "bogus = 1"),
        ("stt.local", 'device = "tpu"'),
        ("stt.local", "beam_size = 0"),
        ("stt", "language_hints = []"),
    ],
)
def test_other_bounds_rejected(tmp_path, section, text):
    with pytest.raises(ConfigError):
        _load(tmp_path, f"[{section}]\n{text}\n")


def test_input_device_forms(tmp_path):
    assert _load(tmp_path, "[voice]\ninput_device = 2\n").voice.input_device == 2
    assert _load(tmp_path, '[voice]\ninput_device = "USB"\n').voice.input_device == "USB"


def test_stt_key_is_secret_and_groq_skipped_without_it():
    s = Settings()
    assert s.available_stt_engines() == ["local"]
    s.with_api_keys({"GROQ_API_KEY": "gsk-secret-123"})
    assert "gsk-secret-123" in s.secret_values()
    assert s.available_stt_engines() == ["groq", "local"]
    assert "gsk-secret-123" not in repr(s)


def test_custom_stt_key_env_registered():
    s = Settings.model_validate({"stt": {"groq": {"api_key_env": "MY_STT_KEY"}}})
    s.with_api_keys({"MY_STT_KEY": "k-1234"})
    assert "k-1234" in s.secret_values()
    assert s.available_stt_engines() == ["groq", "local"]


def test_language_defaults_to_ru():
    assert Settings().stt.language == "ru"


@pytest.mark.parametrize("value", ["ru", "en", "auto", "de"])
def test_language_accepted(tmp_path, value):
    assert _load(tmp_path, f'[stt]\nlanguage = "{value}"\n').stt.language == value


@pytest.mark.parametrize("value", ["", "russian", "r", "ru-RU", "1"])
def test_language_invalid_is_config_error(tmp_path, value):
    with pytest.raises(ConfigError):
        _load(tmp_path, f'[stt]\nlanguage = "{value}"\n')


def test_owner_legacy_stt_config_still_loads(tmp_path, caplog):
    text = (
        '[stt]\nprovider = "faster_whisper"\nmodel = "base"\ndevice = "cuda"\n'
        'compute_type = "float16"\nlanguage_hints = ["ru", "en"]\n'
    )
    with caplog.at_level("WARNING"):
        s = _load(tmp_path, text)
    assert s.stt.language == "ru"
    assert "deprecated [stt] keys" in caplog.text
