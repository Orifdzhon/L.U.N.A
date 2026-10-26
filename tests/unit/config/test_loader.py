from pathlib import Path

import pytest

from luna.config.loader import load_settings
from luna.config.settings import Settings
from luna.core.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_defaults_when_no_file(tmp_path):
    settings = load_settings(env={}, cwd=tmp_path)
    assert settings == Settings()
    assert settings.app.log_level == "INFO"
    assert settings.llm.chain == ["groq", "gemini", "local"]


def test_explicit_path_is_read(tmp_path):
    cfg = tmp_path / "custom.toml"
    cfg.write_text('[app]\nlog_level = "DEBUG"\n', encoding="utf-8")
    assert load_settings(cfg, env={}, cwd=tmp_path).app.log_level == "DEBUG"


def test_explicit_path_missing_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "missing.toml", env={}, cwd=tmp_path)


def test_appdata_beats_cwd(tmp_path):
    appdata = tmp_path / "appdata"
    (appdata / "LUNA").mkdir(parents=True)
    (appdata / "LUNA" / "luna.toml").write_text('[app]\nlog_level = "WARNING"\n', encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "luna.toml").write_text('[app]\nlog_level = "ERROR"\n', encoding="utf-8")
    settings = load_settings(env={"APPDATA": str(appdata)}, cwd=cwd)
    assert settings.app.log_level == "WARNING"


def test_cwd_used_as_last_resort(tmp_path):
    (tmp_path / "luna.toml").write_text('[app]\nlog_level = "ERROR"\n', encoding="utf-8")
    assert load_settings(env={}, cwd=tmp_path).app.log_level == "ERROR"


def _load_text(tmp_path, text, env=None):
    cfg = tmp_path / "luna.toml"
    cfg.write_text(text, encoding="utf-8")
    return load_settings(cfg, env=env or {}, cwd=tmp_path)


def test_invalid_value_raises_config_error_naming_key(tmp_path):
    with pytest.raises(ConfigError, match=r"search\.fetch_pages"):
        _load_text(tmp_path, "[search]\nfetch_pages = -1\n")


def test_invalid_log_level_names_key(tmp_path):
    with pytest.raises(ConfigError, match=r"app\.log_level"):
        _load_text(tmp_path, '[app]\nlog_level = "LOUD"\n')


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="bogus"):
        _load_text(tmp_path, "[app]\nbogus = 1\n")


def test_malformed_toml_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="Invalid TOML"):
        _load_text(tmp_path, "[app\n")


def test_chain_with_unknown_provider_rejected(tmp_path):
    with pytest.raises(ConfigError, match="nope"):
        _load_text(tmp_path, '[llm]\nchain = ["nope"]\n')


def test_partial_provider_override_keeps_other_keys(tmp_path):
    settings = _load_text(tmp_path, '[llm.providers.gemini]\nmodel = "other-model"\n')
    gemini = settings.llm.providers["gemini"]
    assert gemini.model == "other-model"
    assert gemini.api_key_env == "GEMINI_API_KEY"
    assert "groq" in settings.llm.providers


def test_router_aliases_loaded(tmp_path):
    settings = _load_text(tmp_path, '[router.app_aliases]\n"блокнот" = "notepad"\n')
    assert settings.router.app_aliases == {"блокнот": "notepad"}
    assert settings.router.threshold == 85


def test_example_config_parses_and_equals_defaults(tmp_path):
    example = tmp_path / "example.toml"
    example.write_bytes((REPO_ROOT / "luna.example.toml").read_bytes())
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    settings = load_settings(example, env={}, cwd=cwd)
    defaults = Settings()
    for section in ("app", "llm", "search", "router", "tts", "stt", "voice", "wakeword", "ui"):
        assert getattr(settings, section) == getattr(defaults, section)


def test_legacy_owner_config_still_loads(tmp_path, caplog):
    text = (
        '[stt]\nprovider = "faster_whisper"\nmodel = "large-v3-turbo"\ndevice = "cuda"\n'
        'compute_type = "int8_float16"\n\n'
        '[wakeword]\nengine = "vosk"\nkeywords = ["луна"]\nhotkey = "ctrl+alt+space"\n\n'
        "[ui]\nautostart = false\n"
    )
    with caplog.at_level("WARNING"):
        settings = _load_text(tmp_path, text)
    assert settings.stt == Settings().stt
    assert "deprecated [stt] keys" in caplog.text
    assert settings.wakeword == Settings().wakeword
    assert settings.ui == Settings().ui


# --- [tts] -------------------------------------------------------------------------------


def test_tts_defaults():
    tts = Settings().tts
    assert tts.enabled is False
    assert tts.provider == "edge"
    assert (tts.edge_voice_ru, tts.edge_voice_en) == (
        "ru-RU-SvetlanaNeural",
        "en-US-JennyNeural",
    )
    assert (tts.voice_ru, tts.voice_en) == ("Irina", "Zira")
    assert (tts.rate, tts.volume, tts.max_chars, tts.first_chunk_max_chars) == (0, 100, 1500, 200)


def test_tts_values_loaded(tmp_path):
    settings = _load_text(tmp_path, "[tts]\nenabled = true\nrate = -3\nvolume = 40\n")
    assert settings.tts.enabled is True
    assert (settings.tts.rate, settings.tts.volume) == (-3, 40)


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("rate = 11", r"tts\.rate"),
        ("rate = -11", r"tts\.rate"),
        ("volume = 101", r"tts\.volume"),
        ("volume = -1", r"tts\.volume"),
        ("max_chars = 0", r"tts\.max_chars"),
        ("first_chunk_max_chars = 0", r"tts\.first_chunk_max_chars"),
        ('provider = "piper"', r"tts\.provider"),
        ('voice_ru = ""', r"tts\.voice_ru"),
        ("bogus = 1", "bogus"),
    ],
)
def test_tts_invalid_values_rejected(tmp_path, body, key):
    with pytest.raises(ConfigError, match=key):
        _load_text(tmp_path, f"[tts]\n{body}\n")


def test_old_piper_provider_error_has_hint(tmp_path):
    with pytest.raises(ConfigError) as info:
        _load_text(tmp_path, '[tts]\nprovider = "piper"\nvoice_ru = "ru_RU-dmitri-medium"\n')
    message = str(info.value)
    assert "tts.provider" in message
    assert 'Piper is not supported yet; use provider = "sapi"' in message
    assert "docs/SETUP.md#voice-text-to-speech" in message


def test_other_tts_errors_have_no_piper_hint(tmp_path):
    with pytest.raises(ConfigError) as info:
        _load_text(tmp_path, "[tts]\nrate = 11\n")
    assert "Piper" not in str(info.value)


# --- secrets -----------------------------------------------------------------------------


def test_key_from_environment_found(tmp_path):
    settings = load_settings(env={"GEMINI_API_KEY": "test-key-123"}, cwd=tmp_path)
    assert settings.api_key("GEMINI_API_KEY").get_secret_value() == "test-key-123"


def test_key_from_dotenv_found_and_env_wins(tmp_path):
    (tmp_path / ".env").write_text("GROQ_API_KEY=from-file\nGEMINI_API_KEY=file-g\n")
    settings = load_settings(env={"GEMINI_API_KEY": "from-env"}, cwd=tmp_path)
    assert settings.api_key("GROQ_API_KEY").get_secret_value() == "from-file"
    assert settings.api_key("GEMINI_API_KEY").get_secret_value() == "from-env"


def test_dotenv_in_appdata_is_read(tmp_path):
    appdata = tmp_path / "appdata"
    (appdata / "LUNA").mkdir(parents=True)
    (appdata / "LUNA" / ".env").write_text("TAVILY_API_KEY=tav\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    settings = load_settings(env={"APPDATA": str(appdata)}, cwd=cwd)
    assert settings.api_key("TAVILY_API_KEY").get_secret_value() == "tav"


def test_secret_never_in_repr_or_dumps(tmp_path):
    settings = load_settings(env={"GEMINI_API_KEY": "test-key-123"}, cwd=tmp_path)
    assert settings.api_key("GEMINI_API_KEY") is not None
    for text in (
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        str(settings.model_dump()),
    ):
        assert "test-key-123" not in text
    assert "test-key-123" not in repr(settings.api_key("GEMINI_API_KEY"))


def test_non_utf8_dotenv_raises_config_error_without_contents(tmp_path):
    (tmp_path / ".env").write_bytes("GEMINI_API_KEY=test-key-123\n".encode("utf-16"))
    with pytest.raises(ConfigError, match="UTF-8") as exc:
        load_settings(env={}, cwd=tmp_path)
    assert ".env" in str(exc.value)
    assert "test-key-123" not in str(exc.value)


def test_empty_env_var_does_not_override_dotenv(tmp_path):
    (tmp_path / ".env").write_text("GROQ_API_KEY=from-file\n")
    settings = load_settings(env={"GROQ_API_KEY": ""}, cwd=tmp_path)
    assert settings.api_key("GROQ_API_KEY").get_secret_value() == "from-file"


def test_dotenv_values_are_not_interpolated(tmp_path):
    (tmp_path / ".env").write_text("GROQ_API_KEY=a${HOME}b\n")
    settings = load_settings(env={"HOME": "X"}, cwd=tmp_path)
    assert settings.api_key("GROQ_API_KEY").get_secret_value() == "a${HOME}b"


def test_empty_api_key_env_means_no_key_needed(tmp_path):
    settings = _load_text(tmp_path, '[llm.providers.groq]\napi_key_env = ""\n')
    assert settings.api_key("") is None
    assert settings.available_providers() == ["groq", "local"]


def test_api_key_in_toml_is_rejected_without_leaking_it(tmp_path):
    with pytest.raises(ConfigError, match="api_key") as exc:
        _load_text(tmp_path, '[llm.providers.gemini]\napi_key = "test-key-123"\n')
    assert "test-key-123" not in str(exc.value)


def test_blank_key_counts_as_missing(tmp_path):
    (tmp_path / ".env").write_text("GEMINI_API_KEY=\nGROQ_API_KEY=\n")
    settings = load_settings(env={}, cwd=tmp_path)
    assert settings.api_key("GEMINI_API_KEY") is None
    assert settings.available_providers() == ["local"]


def test_router_app_aliases_accept_lists(tmp_path):
    text = (
        '[router.app_aliases]\n"калькулятор" = ["Калькулятор", "Calculator"]\n"хром" = "Chrome"\n'
    )
    settings = _load_text(tmp_path, text)
    assert settings.router.app_aliases == {
        "калькулятор": ["Калькулятор", "Calculator"],
        "хром": "Chrome",
    }
