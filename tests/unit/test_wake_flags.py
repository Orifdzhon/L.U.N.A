"""--wake / --no-wake and their interplay with --listen / --no-listen and luna.toml."""

import pytest
from test_voice_flags import isolated  # noqa: F401

from luna.__main__ import main


def capture(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "luna.__main__._run_text",
        lambda s: seen.append((s.wakeword.enabled, s.voice.listen)) or 0,
    )
    return seen


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--text"], (False, False)),
        (["--text", "--wake"], (True, True)),  # --wake implies voice input
        (["--text", "--wake", "--no-listen"], (True, True)),
        (["--text", "--no-wake"], (False, False)),
    ],
)
def test_wake_flags_vs_defaults(isolated, monkeypatch, argv, expected):  # noqa: F811
    seen = capture(monkeypatch)
    assert main(argv) == 0
    assert seen == [expected]


def test_config_enabled_starts_wake_and_implies_nothing_else(isolated, monkeypatch):  # noqa: F811
    cfg = isolated / "w.toml"
    cfg.write_text("[wakeword]\nenabled = true\n", encoding="utf-8")
    seen = capture(monkeypatch)
    assert main(["--text", "--config", str(cfg)]) == 0
    assert main(["--text", "--config", str(cfg), "--no-wake"]) == 0
    assert main(["--text", "--config", str(cfg), "--no-listen"]) == 0  # no microphone at all
    assert seen == [(True, False), (False, False), (False, False)]


def test_wake_and_no_wake_conflict(isolated):  # noqa: F811
    with pytest.raises(SystemExit) as exc:
        main(["--text", "--wake", "--no-wake"])
    assert exc.value.code == 2


def test_help_lists_wake(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "--wake" in capsys.readouterr().out
