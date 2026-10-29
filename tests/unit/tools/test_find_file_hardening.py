import os
from pathlib import Path

import pytest

from fakes import FakeClock, FakeConfirmer
from files_support import FakeFileIndex, FakeFileOpener, StepClock, fake_hit, make_files
from luna.config.settings import FilesSettings
from luna.files import walk as walk_mod
from luna.files.base import RootDir
from luna.files.query import load_kinds, parse_query
from luna.files.search import FileSearch
from luna.tools.base import ToolContext
from luna.tools.builtin.find_file import (
    FileResults,
    FindFileTool,
    OpenFileTool,
    _hide_user,
)

CTX = ToolContext("r1", "text", FakeConfirmer(), "find")
KINDS = load_kinds()


def test_user_name_never_reaches_the_llm_via_a_drive_root(tmp_path, monkeypatch):
    drive = RootDir(tmp_path, "C:", False)
    make_files(tmp_path, ["Users/bob/Source/cv.pdf", "Users/me/Source/cv2.pdf"])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "Users" / "me"))
    conf = FilesSettings()
    search = FileSearch(lambda: [drive], None, conf, clock=StepClock())
    text = FindFileTool(search, FileResults(), conf, KINDS).run({"query": "cv"}, CTX).summary
    assert "bob" not in text and "Users\\me" not in text
    assert "~\\Source\\cv2.pdf" in text
    assert "C:\\Users\\...\\Source\\cv.pdf" in text  # another user's folder


def test_hide_user_cases():
    assert _hide_user("C:\\Users\\Me\\Source\\cv.pdf", "me") == "~\\Source\\cv.pdf"
    assert _hide_user("c:\\users\\Bob", "me") == "c:\\Users\\..."
    assert _hide_user("D:\\Work\\cv.pdf", "me") == "D:\\Work\\cv.pdf"


@pytest.mark.parametrize("ext", [".svg", ".html", ".htm", ".xhtml", ".mht", ".xml"])
def test_active_content_types_are_reveal_only(tmp_path, ext):
    root = RootDir(tmp_path, "Documents", True)
    (path,) = make_files(tmp_path, [f"pagex{ext}"])
    results, opener, conf = FileResults(clock=FakeClock()), FakeFileOpener(), FilesSettings()
    search = FileSearch(lambda: [root], None, conf, clock=StepClock())
    FindFileTool(search, results, conf, KINDS).run({"query": "pagex"}, CTX)
    out = OpenFileTool(results, opener, KINDS).run({"index": 1}, CTX)
    assert out.data["action"] == "revealed"
    assert opener.revealed == [path] and opener.opened == []


def test_swapped_parent_that_leaves_the_root_is_refused(tmp_path, monkeypatch):
    root = RootDir(tmp_path, "Documents", True)
    make_files(tmp_path, ["sub/notes.pdf"])
    results, opener, conf = FileResults(clock=FakeClock()), FakeFileOpener(), FilesSettings()
    search = FileSearch(lambda: [root], None, conf, clock=StepClock())
    FindFileTool(search, results, conf, KINDS).run({"query": "notes"}, CTX)
    real = os.path.realpath
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda p, **k: "X:\\elsewhere\\notes.pdf" if str(p).endswith("notes.pdf") else real(p),
    )
    out = OpenFileTool(results, opener, KINDS).run({"index": 1}, CTX)
    assert out.data["reason"] == "changed" and opener.opened == []


# -- walk / index filters ---------------------------------------------------------------------


def test_file_symlinks_are_skipped_in_the_walk(tmp_path, monkeypatch):
    make_files(tmp_path, ["report_link.txt", "report_real.txt"])
    monkeypatch.setattr(walk_mod, "_is_link", lambda e: "link" in e.name)
    clock = StepClock()
    result = walk_mod.walk(
        [RootDir(tmp_path, "D")],
        parse_query("report", KINDS),
        deadline=1000,
        max_entries=1000,
        clock=clock,
    )
    assert [h.path.name for h in result.hits] == ["report_real.txt"]


def test_index_hits_through_links_are_dropped(tmp_path, monkeypatch):
    make_files(tmp_path, ["ok/report.txt", "jct/report.txt"])
    monkeypatch.setattr(os.path, "isjunction", lambda p: os.path.basename(str(p)) == "jct")
    assert walk_mod.path_blocked(tmp_path / "jct" / "report.txt", tmp_path)
    assert not walk_mod.path_blocked(tmp_path / "ok" / "report.txt", tmp_path)
    assert walk_mod.path_blocked(tmp_path / "missing" / "x.txt", tmp_path)
    idx = FakeFileIndex(
        [
            fake_hit(str(tmp_path / "jct" / "report.txt")),
            fake_hit(str(tmp_path / "ok" / "report.txt")),
        ]
    )
    search = FileSearch(
        lambda: [RootDir(tmp_path, "A", True)], idx, FilesSettings(), clock=StepClock()
    )
    kept = search._from_index(parse_query("report", KINDS), search._roots(), 4.0)
    assert [h.path.parent.name for h in kept] == ["ok"]
