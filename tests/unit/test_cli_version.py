import subprocess
import sys

import pytest

from luna import __version__
from luna.__main__ import main


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


def test_text_mode_not_implemented():
    assert main(["--text"]) == 2


def test_missing_explicit_config_is_error(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "nope.toml")]) == 2
    assert "config error" in capsys.readouterr().err
