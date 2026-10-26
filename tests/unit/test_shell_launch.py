import sys
from pathlib import Path

from luna.ui.shell.launch import own_launch_command


def test_pythonw_next_to_interpreter_and_fixed_flags(tmp_path):
    cmd = own_launch_command(tmp_path / "venv" / "Scripts" / "python.exe", None, tmp_path)
    assert Path(cmd.executable) == tmp_path / "venv" / "Scripts" / "pythonw.exe"
    assert cmd.args == ("-m", "luna", "--tray", "--minimized")
    assert cmd.working_dir == str(tmp_path)


def test_config_added_only_when_given_and_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = own_launch_command(Path(sys.executable), Path("luna.toml"), tmp_path)
    assert cmd.args[-2] == "--config"
    assert Path(cmd.args[-1]).is_absolute()
    assert Path(cmd.args[-1]) == (tmp_path / "luna.toml").resolve()
    assert cmd.args[:4] == ("-m", "luna", "--tray", "--minimized")


def test_no_config_flag_without_config(tmp_path):
    cmd = own_launch_command(Path(sys.executable), None, tmp_path)
    assert "--config" not in cmd.args
