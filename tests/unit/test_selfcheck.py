import pytest

from luna import __main__ as cli
from luna import selfcheck


def test_all_ok_returns_zero() -> None:
    lines: list[str] = []
    code = selfcheck.run_self_check([("a", lambda: "1 thing")], out=lines.append)
    assert code == 0
    assert lines[0] == "ok   a: 1 thing"
    assert "failed=0" in lines[-1]


def test_failure_returns_one_and_continues() -> None:
    def boom() -> str:
        raise ImportError("no dll")

    lines: list[str] = []
    code = selfcheck.run_self_check([("a", boom), ("b", lambda: "fine")], out=lines.append)
    assert code == 1
    assert lines[0] == "FAIL a: ImportError: no dll"
    assert lines[1].startswith("ok   b")
    assert "failed=1" in lines[-1]


def test_checks_depend_on_edition() -> None:
    public = [name for name, _ in selfcheck.build_checks(has_sherpa=False)]
    personal = [name for name, _ in selfcheck.build_checks(has_sherpa=True)]
    assert "sherpa_onnx" not in public
    assert "sherpa_onnx" in personal
    assert {"site_aliases.toml", "app_aliases.toml", "PySide6"} <= set(public)


def test_alias_checks_report_counts() -> None:
    assert "site aliases" in selfcheck._check_site_aliases()
    assert "app aliases" in selfcheck._check_app_aliases()


def test_cli_option_runs_self_check_before_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "run_self_check", lambda: 7)
    assert cli.main(["--self-check"]) == 7
