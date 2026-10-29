import builtins
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from files_support import FakeFileIndex, LogCapture, StepClock, fake_hit, make_files
from luna.config.settings import FilesSettings
from luna.core.errors import ProviderUnavailable
from luna.files import walk as walk_mod
from luna.files.base import RootDir
from luna.files.query import load_kinds, parse_query
from luna.files.search import FileSearch
from luna.files.walk import DEFAULT_SKIP, walk

KINDS = load_kinds()


def q(text):
    return parse_query(text, KINDS)


def names(result):
    return [h.path.name for h in result.hits]


def run(roots, text, *, budget=1000.0, max_entries=100_000, skip=DEFAULT_SKIP, **kw):
    clock = kw.pop("clock", StepClock())
    return walk(
        roots,
        q(text),
        deadline=clock() + budget,
        max_entries=max_entries,
        skip=skip,
        clock=clock,
        **kw,
    )


def test_finds_files_and_folders_by_name(tmp_path):
    make_files(tmp_path, ["a/Резюме_2026.pdf", "a/other.txt", "Резюме папка/", "b/c/резюме.docx"])
    result = run([RootDir(tmp_path, "Documents", True)], "резюме")
    assert sorted(names(result)) == ["Резюме папка", "Резюме_2026.pdf", "резюме.docx"]
    assert not result.timed_out
    assert {h.root_label for h in result.hits} == {"Documents"}
    pdf = next(h for h in result.hits if h.path.suffix == ".pdf")
    assert pdf.size == 1 and pdf.modified is not None and not pdf.is_dir


def test_kind_filter_in_walk(tmp_path):
    make_files(tmp_path, ["отчет.pdf", "отчет.docx", "отчет проект/"])
    assert names(run([RootDir(tmp_path, "D")], "пдф отчет")) == ["отчет.pdf"]
    assert names(run([RootDir(tmp_path, "D")], "папку отчет")) == ["отчет проект"]


def test_skip_list_is_case_insensitive_and_extendable(tmp_path):
    make_files(
        tmp_path,
        ["Node_Modules/x/report.txt", "AppData/report.txt", "ok/report.txt", "custom/report.txt"],
    )
    root = RootDir(tmp_path, "D")
    found = run([root], "report").hits
    assert sorted(h.path.parent.name for h in found) == ["custom", "ok"]
    skipped = run([root], "report", skip=DEFAULT_SKIP | {"custom"})
    assert [h.path.parent.name for h in skipped.hits] == ["ok"]


def test_bfs_shallow_first(tmp_path):
    make_files(tmp_path, ["a/b/c/target_deep.txt", "target_top.txt", "a/target_mid.txt"])
    assert names(run([RootDir(tmp_path, "D")], "target")) == [
        "target_top.txt",
        "target_mid.txt",
        "target_deep.txt",
    ]


def test_hidden_and_system_entries_skipped(tmp_path, monkeypatch):
    make_files(tmp_path, ["hidden_dir/report.txt", "report_hidden.txt", "report_ok.txt"])
    real_stat = os.DirEntry.stat

    class Entry:
        """Wraps a DirEntry so that names containing "hidden" carry the hidden attribute."""

        def __init__(self, entry):
            self._e = entry
            self.name, self.path = entry.name, entry.path

        def is_dir(self, **kw):
            return self._e.is_dir(**kw)

        def is_symlink(self):
            return False

        def is_junction(self):
            return False

        def stat(self, **kw):
            st = real_stat(self._e, **kw)
            attrs = 0x2 if "hidden" in self.name else 0
            return SimpleNamespace(
                st_file_attributes=attrs, st_size=st.st_size, st_mtime=st.st_mtime
            )

    real_scandir = os.scandir

    class Wrapped:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()

        def __iter__(self):
            return (Entry(e) for e in self._it)

    monkeypatch.setattr(walk_mod.os, "scandir", Wrapped)
    assert names(run([RootDir(tmp_path, "D")], "report")) == ["report_ok.txt"]


@pytest.mark.skipif(sys.platform != "win32", reason="file attributes are a Windows feature")
def test_real_hidden_attribute_skipped(tmp_path):
    import ctypes

    paths = make_files(tmp_path, ["report_hidden.txt", "report_ok.txt"])
    assert ctypes.windll.kernel32.SetFileAttributesW(str(paths[0]), 0x2)
    assert names(run([RootDir(tmp_path, "D")], "report")) == ["report_ok.txt"]


def test_symlinks_and_junctions_not_followed(tmp_path, monkeypatch):
    make_files(tmp_path, ["real/report.txt", "loop/report_inner.txt"])
    monkeypatch.setattr(walk_mod, "_is_link", lambda e: e.name == "loop")
    assert names(run([RootDir(tmp_path, "D")], "report")) == ["report.txt"]


def test_symlink_real(tmp_path):
    make_files(tmp_path, ["real/report.txt"])
    try:
        os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert names(run([RootDir(tmp_path, "D")], "report")) == ["report.txt"]


def test_other_roots_are_skipped_inside_a_root(tmp_path):
    make_files(tmp_path, ["docs/report.txt", "x/report2.txt"])
    outer, inner = RootDir(tmp_path, "C:"), RootDir(tmp_path / "docs", "Documents", True)
    result = run([inner, outer], "report")
    assert sorted((h.root_label, h.path.name) for h in result.hits) == [
        ("C:", "report2.txt"),
        ("Documents", "report.txt"),
    ]


def test_deadline_stops_with_timed_out(tmp_path):
    make_files(tmp_path, [f"d{i}/f.txt" for i in range(20)])
    clock = StepClock(step=1.0)
    result = run([RootDir(tmp_path, "D")], "nothing", budget=5.0, clock=clock)
    assert result.timed_out and not result.hits


def test_max_entries_stops_with_timed_out(tmp_path):
    make_files(tmp_path, [f"f{i}.txt" for i in range(30)])
    result = run([RootDir(tmp_path, "D")], "nothing", max_entries=10)
    assert result.timed_out and result.scanned <= 11


def test_complete_walk_is_not_timed_out(tmp_path):
    make_files(tmp_path, ["a/x.txt"])
    assert not run([RootDir(tmp_path, "D")], "zzz").timed_out


def test_cancel_event_honoured(tmp_path):
    make_files(tmp_path, ["a/report.txt"])
    cancel = threading.Event()
    cancel.set()
    result = run([RootDir(tmp_path, "D")], "report", cancel=cancel)
    assert result.cancelled and not result.hits


def test_each_root_gets_a_share_of_the_time(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    make_files(a, [f"d{i}/x.txt" for i in range(50)])
    make_files(b, ["report.txt"])
    clock = StepClock(step=1.0)
    result = run([RootDir(a, "A"), RootDir(b, "B")], "report", budget=20.0, clock=clock)
    assert names(result) == ["report.txt"]  # the big first root did not eat the second one's time


def test_never_opens_files(tmp_path, monkeypatch):
    make_files(tmp_path, ["secret_report.txt"])

    def forbidden(*a, **k):
        raise AssertionError("a file was opened")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert names(run([RootDir(tmp_path, "D")], "report")) == ["secret_report.txt"]


def test_unreadable_directory_is_skipped(tmp_path, monkeypatch):
    make_files(tmp_path, ["locked/report.txt", "report_ok.txt"])
    real = os.scandir

    def scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError(path)
        return real(path)

    monkeypatch.setattr(walk_mod.os, "scandir", scandir)
    assert names(run([RootDir(tmp_path, "D")], "report")) == ["report_ok.txt"]


# -- FileSearch -------------------------------------------------------------------------------


def search_for(roots, index=None, **conf):
    return FileSearch(lambda: roots, index, FilesSettings(**conf), clock=StepClock())


def test_search_walk_only_ranks_and_limits(tmp_path):
    make_files(tmp_path, ["Резюме_2026.pdf", "резюмирование.txt", "a/b/resume.docx"])
    out = search_for([RootDir(tmp_path, "Documents", True)], max_results=2).search(q("резюме"))
    assert out.backend == "walk" and not out.timed_out
    assert out.hits[0].path.name == "Резюме_2026.pdf"
    assert len(out.hits) == 2
    assert out.roots[0].label == "Documents"


def test_user_roots_are_walked_before_drives(tmp_path, monkeypatch):
    seen = []

    def fake_walk(roots, *a, **k):
        seen.extend(r.label for r in roots)
        return walk_mod.WalkResult()

    monkeypatch.setattr("luna.files.search.walk", fake_walk)
    roots = [
        RootDir(tmp_path / "drive", "D:", False),
        RootDir(tmp_path / "home", "Documents", True),
        RootDir(tmp_path / "more", "Music", True),
    ]
    search_for(roots).search(q("report"))
    assert seen == ["Documents", "Music", "D:"]


def test_index_hits_merge_and_dedupe_with_walk(tmp_path):
    make_files(tmp_path / "a", ["report_a.txt"])
    make_files(tmp_path / "b", ["report_b.txt"])
    roots = [RootDir(tmp_path / "a", "A", True), RootDir(tmp_path / "b", "B", True)]
    idx = FakeFileIndex([fake_hit(str(tmp_path / "a" / "report_a.txt"), label="?")])
    out = search_for(roots, idx).search(q("report"))
    assert out.backend == "index+walk"
    assert sorted(h.path.name for h in out.hits) == ["report_a.txt", "report_b.txt"]
    assert next(h for h in out.hits if h.path.name == "report_a.txt").root_label == "A"
    assert len(idx.calls) == 1


def test_fully_covered_roots_are_not_walked(tmp_path, monkeypatch):
    make_files(tmp_path, ["report.txt"])
    monkeypatch.setattr(
        "luna.files.search.walk", lambda *a, **k: pytest.fail("walked a covered root")
    )
    idx = FakeFileIndex([fake_hit(str(tmp_path / "report.txt"))])
    out = search_for([RootDir(tmp_path, "A", True)], idx).search(q("report"))
    assert out.backend == "index" and len(out.hits) == 1


def test_index_hits_outside_roots_or_in_skipped_dirs_dropped(tmp_path):
    root = tmp_path / "r"
    make_files(root, ["ok/report.txt"])  # stale index hits (files gone) are dropped too
    idx = FakeFileIndex(
        [
            fake_hit(str(tmp_path / "elsewhere" / "report.txt")),
            fake_hit(str(root / "node_modules" / "p" / "report.txt")),
            fake_hit(str(root / "ok" / "report.txt")),
        ]
    )
    out = search_for([RootDir(root, "A", True)], idx).search(q("report"))
    assert [h.path.parent.name for h in out.hits] == ["ok"]


@pytest.mark.parametrize(
    "idx",
    [
        FakeFileIndex(available=False),
        FakeFileIndex(error=ProviderUnavailable("down")),
        FakeFileIndex(error=RuntimeError("boom")),
    ],
)
def test_broken_index_falls_back_to_the_walk(tmp_path, idx):
    make_files(tmp_path, ["report.txt"])
    out = search_for([RootDir(tmp_path, "A", True)], idx).search(q("report"))
    assert [h.path.name for h in out.hits] == ["report.txt"]
    assert out.backend == "walk"


def test_search_reports_time_budget_exhaustion(tmp_path):
    make_files(tmp_path, [f"d{i}/f.txt" for i in range(10)])
    search = FileSearch(
        lambda: [RootDir(tmp_path, "A", True)],
        None,
        FilesSettings(time_budget_s=2.0),
        clock=StepClock(step=1.0),
    )
    assert search.search(q("nothing")).timed_out


def test_search_cancelled_before_start_walks_nothing(tmp_path):
    make_files(tmp_path, ["report.txt"])
    cancel = threading.Event()
    cancel.set()
    out = search_for([RootDir(tmp_path, "A", True)]).search(q("report"), cancel)
    assert not out.hits


def test_search_logs_counts_only(tmp_path):
    make_files(tmp_path, ["СекретныйОтчет_Иванов.pdf"])
    with LogCapture() as logs:
        out = search_for([RootDir(tmp_path, "A", True)]).search(q("отчет"))
    assert len(out.hits) == 1
    assert "1 result" in logs.text and "backend=walk" in logs.text
    assert "Секретный" not in logs.text and str(tmp_path) not in logs.text


def test_extra_exclude_setting(tmp_path):
    make_files(tmp_path, ["Private/report.txt", "pub/report.txt"])
    out = search_for([RootDir(tmp_path, "A", True)], exclude=["private"]).search(q("report"))
    assert [h.path.parent.name for h in out.hits] == ["pub"]
