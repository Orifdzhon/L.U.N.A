"""Command line modes: tray by default, --text CLI, single instance, no Qt in --text."""

import logging
import os
import subprocess
import sys

import pytest

from luna.__main__ import EXIT_ALREADY_RUNNING, build_parser, main
from luna.logs.redact import clear_secrets


class Taken:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    def acquire(self):
        self.acquired += 1
        return False

    def release(self):
        self.released += 1


class Free(Taken):
    def acquire(self):
        self.acquired += 1
        return True


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.chdir(tmp_path)
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


def spy_modes(monkeypatch, instance):
    seen = []
    monkeypatch.setattr("luna.__main__.create_single_instance", lambda: instance)
    monkeypatch.setattr("luna.__main__._run_text", lambda s: seen.append("text") or 0)
    monkeypatch.setattr("luna.__main__._run_tray", lambda s, a, i: seen.append(("tray", a)) or 0)
    return seen


def test_parser_flags():
    parser = build_parser()
    args = parser.parse_args([])
    assert not args.text and not args.tray and not args.minimized and args.config is None
    args = parser.parse_args(["--tray", "--minimized", "--config", "x.toml"])
    assert args.tray and args.minimized and str(args.config) == "x.toml"


def test_default_mode_is_tray(isolated, monkeypatch):
    instance = Free()
    seen = spy_modes(monkeypatch, instance)
    assert main([]) == 0
    assert [s[0] for s in seen] == ["tray"] and not seen[0][1].minimized
    assert instance.released == 1
    assert main(["--minimized"]) == 0
    assert seen[1][1].minimized


def test_explicit_tray_and_text(isolated, monkeypatch):
    seen = spy_modes(monkeypatch, Free())
    assert main(["--tray"]) == 0
    assert main(["--text"]) == 0
    assert [s if isinstance(s, str) else s[0] for s in seen] == ["tray", "text"]


def test_text_with_tray_is_usage_error(isolated, monkeypatch):
    seen = spy_modes(monkeypatch, Free())
    with pytest.raises(SystemExit) as exc:
        main(["--text", "--tray"])
    assert exc.value.code == 2 and seen == []


def test_config_is_carried_to_tray(isolated, monkeypatch):
    cfg = isolated / "c.toml"
    cfg.write_text("[ui]\nstart_minimized = true\n", encoding="utf-8")
    seen = spy_modes(monkeypatch, Free())
    assert main(["--config", str(cfg)]) == 0
    assert seen[0][1].config == cfg


def test_second_instance_text_mode_exits_3(isolated, monkeypatch, capsys):
    instance = Taken()
    seen = spy_modes(monkeypatch, instance)
    assert main(["--text"]) == EXIT_ALREADY_RUNNING == 3
    assert "already running" in capsys.readouterr().err
    assert seen == [] and instance.released == 0


def test_second_instance_tray_mode_shows_message_box(isolated, monkeypatch):
    seen = spy_modes(monkeypatch, Taken())
    shown = []
    monkeypatch.setattr("luna.ui.qt.app.show_already_running", lambda: shown.append(1))
    assert main([]) == 3
    assert shown == [1] and seen == []


def test_windowless_start_installs_hooks_after_logging(isolated, monkeypatch):
    spy_modes(monkeypatch, Free())
    order = []
    monkeypatch.setattr("luna.__main__.setup_logging", lambda *a, **k: order.append("log"))
    monkeypatch.setattr("luna.logs.windowless.install", lambda *a, **k: order.append("install"))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["--tray"]) == 0
    assert order == ["log", "install"]


def test_text_mode_does_not_import_pyside(isolated):
    code = (
        "import sys, luna.__main__ as m\n"
        "from luna.platform.fallback import ProcessLocalInstance\n"
        "m.create_single_instance = ProcessLocalInstance\n"
        "m._run_text = lambda s: 0\n"
        "assert m.main(['--text']) == 0\n"
        "assert 'PySide6' not in sys.modules\n"
    )
    env = {
        **os.environ,
        "APPDATA": str(isolated / "appdata"),
        "LOCALAPPDATA": str(isolated / "localappdata"),
    }
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=isolated
    )
    assert result.returncode == 0, result.stderr
