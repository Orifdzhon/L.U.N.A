import logging

import pytest

from luna.__main__ import main
from luna.logs.redact import clear_secrets


@pytest.fixture
def isolated(monkeypatch, tmp_path):
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


def capture(monkeypatch):
    seen = []
    monkeypatch.setattr("luna.__main__._run_text", lambda s: seen.append(s.tts.enabled) or 0)
    return seen


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["--text"], False), (["--text", "--voice"], True), (["--text", "--no-voice"], False)],
)
def test_flags_vs_default(isolated, monkeypatch, argv, expected):
    seen = capture(monkeypatch)
    assert main(argv) == 0
    assert seen == [expected]


def test_flags_override_config(isolated, monkeypatch):
    cfg = isolated / "v.toml"
    cfg.write_text("[tts]\nenabled = true\n", encoding="utf-8")
    seen = capture(monkeypatch)
    assert main(["--text", "--config", str(cfg)]) == 0
    assert main(["--text", "--no-voice", "--config", str(cfg)]) == 0
    assert seen == [True, False]


def test_voice_and_no_voice_conflict(isolated):
    with pytest.raises(SystemExit) as exc:
        main(["--text", "--voice", "--no-voice"])
    assert exc.value.code == 2


def test_help_lists_voice(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "--voice" in capsys.readouterr().out
