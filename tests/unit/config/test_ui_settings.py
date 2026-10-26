import pytest

from luna.config.loader import load_settings
from luna.config.settings import Settings, UISettings
from luna.core.errors import ConfigError


def _load(tmp_path, text):
    path = tmp_path / "luna.toml"
    path.write_text(text, encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    return load_settings(path, env={}, cwd=cwd)


def test_defaults_without_section(tmp_path):
    ui = _load(tmp_path, "").ui
    assert ui == UISettings() == Settings().ui
    assert (ui.start_minimized, ui.notifications, ui.history_lines) == (False, True, 200)


@pytest.mark.parametrize("value", [19, 2001, 0, -5])
def test_history_lines_out_of_range(tmp_path, value):
    with pytest.raises(ConfigError):
        _load(tmp_path, f"[ui]\nhistory_lines = {value}\n")


@pytest.mark.parametrize("value", [20, 2000])
def test_history_lines_bounds_ok(tmp_path, value):
    assert _load(tmp_path, f"[ui]\nhistory_lines = {value}\n").ui.history_lines == value


def test_legacy_autostart_false_is_silent(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        settings = _load(tmp_path, "[ui]\nautostart = false\n")
    assert settings.ui == UISettings()
    assert "autostart" not in caplog.text
    assert "autostart" not in settings.ui.model_dump()


def test_legacy_autostart_true_warns_once(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        settings = _load(tmp_path, "[ui]\nautostart = true\n")
    records = [r for r in caplog.records if "autostart" in r.getMessage()]
    assert len(records) == 1
    assert "Start with Windows" in records[0].getMessage()
    assert settings.ui == UISettings()


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError):
        _load(tmp_path, "[ui]\ntheme = 'dark'\n")
