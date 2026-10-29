from __future__ import annotations

from luna.ui.shell.firstrun import default_state_path, is_first_run_done, mark_first_run_done


def test_marker_roundtrip_and_keeps_other_keys(tmp_path):
    path = tmp_path / "LUNA" / "state.json"
    assert not is_first_run_done(path)
    path.parent.mkdir()
    path.write_text('{"other": 1}', encoding="utf-8")
    mark_first_run_done(path)
    assert is_first_run_done(path)
    assert '"other": 1' in path.read_text(encoding="utf-8")


def test_broken_or_foreign_json_counts_as_not_done(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert not is_first_run_done(path)
    path.write_text("[1, 2]", encoding="utf-8")
    assert not is_first_run_done(path)
    mark_first_run_done(path)
    assert is_first_run_done(path)


def test_unwritable_location_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    mark_first_run_done(blocker / "state.json")  # parent is a file: OSError is swallowed


def test_default_path_uses_localappdata(tmp_path):
    path = default_state_path({"LOCALAPPDATA": str(tmp_path)})
    assert path == tmp_path / "LUNA" / "state.json"
