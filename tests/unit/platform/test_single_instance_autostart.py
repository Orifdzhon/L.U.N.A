import subprocess
import sys
from pathlib import Path

import pytest

from luna.core.errors import ToolError
from luna.platform import factory
from luna.platform.base import Autostart, LaunchCommand, SingleInstance
from luna.platform.fallback import NoAutostart, ProcessLocalInstance
from luna.platform.windows import autostart as au
from luna.platform.windows.autostart import SHORTCUT_NAME, StartupFolderAutostart
from luna.platform.windows.single_instance import (
    ERROR_ALREADY_EXISTS,
    NamedMutexInstance,
)


class FakeKernel32:
    def __init__(self, error: int = 0, handle: int | None = 77) -> None:
        self.error = error
        self.handle = handle
        self.names: list[str] = []
        self.closed: list[int] = []

    def create_mutex(self, name: str) -> tuple[int | None, int]:
        self.names.append(name)
        return self.handle, self.error

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)


def test_mutex_first_acquire_true_and_release_idempotent():
    k = FakeKernel32()
    inst = NamedMutexInstance(kernel32_factory=lambda: k)
    assert isinstance(inst, SingleInstance)
    assert inst.acquire() is True
    assert inst.acquire() is True  # already held by us
    assert k.names == ["Local\\LUNA-instance"]
    inst.release()
    inst.release()
    assert k.closed == [77]


def test_mutex_already_exists_returns_false_and_closes_handle():
    k = FakeKernel32(error=ERROR_ALREADY_EXISTS)
    inst = NamedMutexInstance(kernel32_factory=lambda: k)
    assert inst.acquire() is False
    assert k.closed == [77]
    inst.release()
    assert k.closed == [77]


def test_mutex_release_without_acquire_is_noop():
    NamedMutexInstance(kernel32_factory=lambda: FakeKernel32()).release()


def test_mutex_creation_failure_does_not_block():
    inst = NamedMutexInstance(kernel32_factory=lambda: FakeKernel32(error=5, handle=None))
    assert inst.acquire() is True


class FakeWriter:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[Path, LaunchCommand]] = []

    def __call__(self, lnk: Path, command: LaunchCommand) -> None:
        if self.fail:
            raise self.fail
        self.calls.append((lnk, command))
        lnk.write_bytes(b"lnk")


CMD = LaunchCommand("C:\\luna\\luna.exe", ("--tray",), "C:\\luna")


def test_enable_creates_only_named_lnk(tmp_path):
    w = FakeWriter()
    a = StartupFolderAutostart(tmp_path / "Startup", writer=w)
    assert isinstance(a, Autostart)
    assert a.is_enabled() is False
    a.enable(CMD)
    assert a.is_enabled() is True
    assert [p.name for p in (tmp_path / "Startup").iterdir()] == [SHORTCUT_NAME] == ["L.U.N.A.lnk"]
    assert w.calls == [(tmp_path / "Startup" / SHORTCUT_NAME, CMD)]
    a.enable(CMD)  # update in place
    assert len(list((tmp_path / "Startup").iterdir())) == 1


def test_disable_removes_only_that_file_and_is_idempotent(tmp_path):
    other = tmp_path / "other.lnk"
    other.write_bytes(b"x")
    a = StartupFolderAutostart(tmp_path, writer=FakeWriter())
    a.enable(CMD)
    a.disable()
    a.disable()
    assert a.is_enabled() is False
    assert other.exists()


def test_writer_failure_raises_tool_error(tmp_path):
    a = StartupFolderAutostart(tmp_path, writer=FakeWriter(fail=RuntimeError("boom")))
    with pytest.raises(ToolError):
        a.enable(CMD)
    assert a.is_enabled() is False


def test_empty_executable_refused(tmp_path):
    with pytest.raises(ToolError):
        StartupFolderAutostart(tmp_path, writer=FakeWriter()).enable(LaunchCommand(" "))


def test_is_enabled_never_raises(monkeypatch, tmp_path):
    a = StartupFolderAutostart(tmp_path, writer=FakeWriter())

    def boom(self):
        raise OSError("denied")

    monkeypatch.setattr(Path, "is_file", boom)
    assert a.is_enabled() is False


def test_powershell_writer_passes_data_via_env_only(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    lnk = tmp_path / SHORTCUT_NAME
    cmd = LaunchCommand("C:\\Program Files\\L\\luna.exe", ("--tray", "a b"), "C:\\work dir")
    au.powershell_writer(lnk, cmd)
    argv, env = seen["argv"], seen["kw"]["env"]
    assert argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert seen["kw"]["timeout"] == 10.0
    assert env["LUNA_LNK"] == str(lnk)
    assert env["LUNA_TARGET"] == cmd.executable
    assert env["LUNA_ARGS"] == subprocess.list2cmdline(["--tray", "a b"])
    assert env["LUNA_CWD"] == "C:\\work dir"
    script = argv[4]
    for value in (str(lnk), cmd.executable, "--tray", "C:\\work dir"):
        assert value not in script


def test_powershell_writer_timeout_and_failure(monkeypatch, tmp_path):
    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(au.subprocess, "run", timeout)
    with pytest.raises(ToolError, match="timed out"):
        au.powershell_writer(tmp_path / "x.lnk", CMD)

    def failed(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "no access")

    monkeypatch.setattr(au.subprocess, "run", failed)
    with pytest.raises(ToolError, match="no access"):
        au.powershell_writer(tmp_path / "x.lnk", CMD)

    def missing(argv, **kw):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(au.subprocess, "run", missing)
    with pytest.raises(ToolError):
        au.powershell_writer(tmp_path / "x.lnk", CMD)


def test_default_startup_dir_falls_back_to_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(au, "_known_startup_folder", lambda: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert au.default_startup_dir() == (
        tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def test_default_startup_dir_prefers_known_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(au, "_known_startup_folder", lambda: tmp_path)
    assert au.default_startup_dir() == tmp_path


def test_fallbacks():
    na = NoAutostart()
    assert na.is_enabled() is False
    na.disable()
    with pytest.raises(ToolError):
        na.enable(CMD)
    pl = ProcessLocalInstance()
    assert pl.acquire() is True
    pl.release()
    assert isinstance(na, Autostart)
    assert isinstance(pl, SingleInstance)


def test_factory_non_windows(monkeypatch):
    monkeypatch.setattr(factory.sys, "platform", "linux")
    assert isinstance(factory.create_autostart(), NoAutostart)
    assert isinstance(factory.create_single_instance(), ProcessLocalInstance)


def test_factory_windows(monkeypatch):
    monkeypatch.setattr(factory.sys, "platform", "win32")
    assert isinstance(factory.create_autostart(), StartupFolderAutostart)
    assert isinstance(factory.create_single_instance(), NamedMutexInstance)


@pytest.mark.desktop
@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
def test_real_writer_creates_lnk_in_tmp_path(tmp_path):
    a = StartupFolderAutostart(tmp_path / "Startup")
    a.enable(LaunchCommand(sys.executable, ("-m", "luna"), str(tmp_path)))
    assert a.is_enabled()
    a.disable()
    assert not a.is_enabled()
