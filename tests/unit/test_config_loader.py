import pytest
from pydantic import ValidationError

from luna.config.loader import load_settings
from luna.config.settings import Settings


def test_defaults_when_no_file(tmp_path):
    settings = load_settings(env={}, cwd=tmp_path)
    assert settings == Settings()
    assert settings.app.log_level == "INFO"


def test_explicit_path_is_read(tmp_path):
    cfg = tmp_path / "custom.toml"
    cfg.write_text('[app]\nlog_level = "DEBUG"\n', encoding="utf-8")
    assert load_settings(cfg, env={}, cwd=tmp_path).app.log_level == "DEBUG"


def test_explicit_path_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
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


def test_invalid_value_rejected(tmp_path):
    cfg = tmp_path / "bad.toml"
    cfg.write_text('[app]\nlog_level = "LOUD"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(cfg, env={}, cwd=tmp_path)
