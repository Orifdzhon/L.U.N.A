import logging
import subprocess
import sys

import pytest

from luna import __version__
from luna.__main__ import main
from luna.logs.redact import clear_secrets


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """No real APPDATA/cwd config; root logger restored after the test."""
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.chdir(cwd)
    clear_secrets()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield tmp_path
    clear_secrets()
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def test_version_flag_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"luna {__version__}"


def test_python_dash_m_version():
    result = subprocess.run(
        [sys.executable, "-m", "luna", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"luna {__version__}"


def test_text_mode_not_implemented(isolated, capsys):
    assert main(["--text"]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_valid_config_file_applies_log_level(isolated):
    cfg = isolated / "ok.toml"
    cfg.write_text('[app]\nlog_level = "DEBUG"\n', encoding="utf-8")
    assert main(["--config", str(cfg)]) == 0
    assert logging.getLogger().level == logging.DEBUG


def test_missing_explicit_config_is_error(isolated, capsys):
    assert main(["--config", str(isolated / "nope.toml")]) == 2
    assert "config error" in capsys.readouterr().err


def test_invalid_toml_is_error_with_path(isolated, capsys):
    cfg = isolated / "broken.toml"
    cfg.write_text("[app\nlog_level = ", encoding="utf-8")
    assert main(["--config", str(cfg)]) == 2
    err = capsys.readouterr().err
    assert "config error" in err
    assert "broken.toml" in err


def test_invalid_value_is_error(isolated, capsys):
    cfg = isolated / "bad.toml"
    cfg.write_text('[app]\nlog_level = "LOUD"\n', encoding="utf-8")
    assert main(["--config", str(cfg)]) == 2
    assert "config error" in capsys.readouterr().err
