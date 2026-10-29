# ruff: noqa: RUF001
import sys
from pathlib import Path

import pytest

from fakes import FakeBrowser, FakeLLM
from files_support import FakeFileOpener, make_files
from luna.app import build_app
from luna.config.settings import FilesSettings, Settings
from luna.core.errors import ToolError
from luna.core.models import Request
from luna.files.base import RootDir
from luna.files.roots import is_unc, resolve_roots
from luna.llm.base import ChatResponse, ToolCall
from luna.platform.factory import create_file_opener, create_roots_provider
from luna.platform.windows.files import ShellFileOpener


def test_files_settings_defaults_and_example_toml():
    f = Settings().files
    assert f.roots == ["user", "drives"] and f.time_budget_s == 4.0
    assert f.max_entries == 300_000 and f.max_results == 5 and f.send_names_to_llm is True
    with pytest.raises(ValueError):
        FilesSettings(max_results=11)
    with pytest.raises(ValueError):
        FilesSettings(unknown=1)


def test_resolve_roots_user_extra_onedrive_drives(tmp_path):
    docs, od, extra, drive = (tmp_path / n for n in ("docs", "od", "extra", "drive"))
    for p in (docs, od, extra, drive):
        p.mkdir()
    conf = FilesSettings(extra_roots=[extra, tmp_path / "missing"])
    roots = resolve_roots(
        conf,
        known_folders=lambda: [("Documents", docs), ("Music", tmp_path / "nope")],
        drives=lambda: [drive, docs],  # a duplicate is dropped
        environ={"OneDrive": str(od), "OneDriveConsumer": str(od)},
    )
    assert [r.label for r in roots[:3]] == ["Documents", "OneDrive", "extra"]
    assert [r.user for r in roots] == [True, True, True, False]
    assert [r.path for r in roots] == [docs, od, extra, drive]


def test_resolve_roots_respects_the_roots_setting(tmp_path):
    drive = tmp_path / "d"
    drive.mkdir()
    only_drives = resolve_roots(
        FilesSettings(roots=["drives"]),
        known_folders=lambda: [("Docs", drive)],
        drives=lambda: [drive],
        environ={},
    )
    assert [r.user for r in only_drives] == [False]
    only_user = resolve_roots(
        FilesSettings(roots=["user"]),
        known_folders=lambda: [("Docs", drive)],
        drives=lambda: [drive],
        environ={},
    )
    assert [r.user for r in only_user] == [True]


def test_unc_paths_never_become_roots(tmp_path):
    assert is_unc("\\\\server\\share") and is_unc("//server/share") and not is_unc("C:\\x")
    roots = resolve_roots(
        FilesSettings(roots=["user"]),
        known_folders=lambda: [("Net", Path("\\\\server\\share"))],
        drives=lambda: [],
        environ={},
    )
    assert roots == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows folders")
def test_real_known_folders_and_drives_are_directories():
    from luna.platform.windows.files import fixed_drives, known_folders

    folders = known_folders()
    assert folders and all(p.is_absolute() for _, p in folders)  # no paths printed
    assert all(p.is_absolute() for p in fixed_drives())
    roots = create_roots_provider(FilesSettings())()
    assert roots and all(isinstance(r, RootDir) for r in roots)


def test_factory_opener_matches_platform():
    opener = create_file_opener()
    assert (opener is not None) == (sys.platform == "win32")


# -- ShellFileOpener (no real Explorer / shell calls) -------------------------------------------


def test_open_uses_startfile_without_arguments():
    started = []
    ShellFileOpener(startfile=started.append).open(Path("C:/x/a.pdf"))
    assert started == [str(Path("C:/x/a.pdf"))]


def test_open_failure_becomes_tool_error():
    def boom(path):
        raise OSError("no association")

    with pytest.raises(ToolError):
        ShellFileOpener(startfile=boom).open(Path("C:/x/a.pdf"))


def test_reveal_prefers_the_shell_call_then_falls_back_to_explorer_without_shell():
    spawned = []
    ok = ShellFileOpener(select=lambda p: True, spawn=spawned.append)
    ok.reveal(Path("C:/x/a.exe"))
    assert spawned == []
    for select in (lambda p: False, lambda p: (_ for _ in ()).throw(OSError("com"))):
        spawned.clear()
        ShellFileOpener(select=select, spawn=spawned.append).reveal(Path("C:/x/a.exe"))
        assert len(spawned) == 1
        argv = spawned[0]
        assert argv[0].lower().endswith("explorer.exe")
        assert argv[1:] == ["/select,", str(Path("C:/x/a.exe"))]


def test_reveal_fallback_failure_is_tool_error():
    def boom(argv):
        raise OSError("nope")

    with pytest.raises(ToolError):
        ShellFileOpener(select=lambda p: False, spawn=boom).reveal(Path("C:/x/a.exe"))


# -- app wiring -----------------------------------------------------------------------------------


def call(name, **args):
    return ChatResponse(None, (ToolCall("c1", name, dict(args)),), "fake", "m", "tool_calls")


def say(text):
    return ChatResponse(text, (), "fake", "m", "stop")


def test_find_then_open_through_the_assistant_and_clear(tmp_path):
    docs = tmp_path / "Documents"
    (path,) = make_files(docs, ["Резюме_2026.pdf"])
    opener = FakeFileOpener()
    llm = FakeLLM(
        [
            call("find_file", query="резюме"),
            say("Нашла один файл."),  # tools are off after untrusted content
            call("open_file", index=1),
            say("Открыла."),
            call("open_file", index=1),
            say("Не вышло."),
        ]
    )
    app = build_app(
        Settings(),
        llm=llm,
        browser=FakeBrowser(),
        file_opener=opener,
        file_roots=lambda: [RootDir(docs, "Documents", True)],
        log_dir=tmp_path / "logs",
        platform="linux",
    )
    try:
        assert {"find_file", "open_file"} <= set(app.tool_names)
        first = app.orchestrator.handle(Request("найди резюме", "text"))
        assert first.text == "Нашла один файл."
        assert opener.opened == []  # nothing is opened in the same turn
        second = app.orchestrator.handle(Request("открой первый", "text"))
        assert second.text == "Открыла." and opener.opened == [path]
        second_system = llm.calls[2].messages[0].content
        assert "[answer based on untrusted tool data]" in second_system
        history = [m.content for m in llm.calls[2].messages if m.role == "assistant"]
        assert history == ["[answer based on untrusted tool data] Нашла один файл."]
        app.assistant.clear_history()  # /clear also forgets the found files
        third = app.orchestrator.handle(Request("открой первый", "text"))
        assert opener.opened == [path]
        assert third.actions[0].data["reason"] == "no_results"
    finally:
        app.close()


def test_file_tools_disabled_without_an_opener_off_windows(tmp_path):
    app = build_app(
        Settings(), llm=FakeLLM([]), browser=FakeBrowser(), platform="linux", log_dir=tmp_path
    )
    try:
        assert "find_file" not in app.tool_names and "open_file" not in app.tool_names
        assert any("find file" in w for w in app.warnings)
    finally:
        app.close()


def test_clear_hook_failure_does_not_break_clear():
    from fakes import FakeLLM as Llm
    from luna.core.assistant import Assistant
    from luna.logs.action_log import ActionLog
    from luna.tools.registry import ToolRegistry

    assistant = Assistant(Llm([]), ToolRegistry(ActionLog(Path("unused.jsonl"))))
    calls = []
    assistant.add_clear_hook(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assistant.add_clear_hook(lambda: calls.append(1))
    assistant.clear_history()
    assert calls == [1]
