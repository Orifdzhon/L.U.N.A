"""WindowsRunningApps over a fake Win32 world: nothing here touches a real process or window."""

import pytest

from fakes import FakeClock
from luna.core.errors import ToolError
from luna.platform import factory
from luna.platform.base import AppEntry, CloseOutcome, ProtectedApp, RunningApps
from luna.platform.fallback import NoRunningApps
from luna.platform.windows.processes import (
    ProcInfo,
    WindowInfo,
    WindowsRunningApps,
    is_protected_path,
)

ROOT = "C:\\Windows"
OWN, PARENT = 100, 90
CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
TG = "D:\\Apps\\Telegram\\Telegram.exe"


def win(
    hwnd, pid, cls="Chrome_WidgetWin_1", *, visible=True, owned=False, titled=True, cloaked=False
):
    return WindowInfo(hwnd, pid, cls, visible, owned, titled, cloaked)


def proc(pid, path, creation=1000, session=1, aumid=None):
    return ProcInfo(pid, path, creation + pid, session, aumid)


class World:
    """Mutable fake machine shared by the four fake wrappers."""

    def __init__(self):
        self.windows: list[WindowInfo] = []
        self.children: dict[int, list[WindowInfo]] = {}
        self.procs: dict[int, ProcInfo] = {OWN: proc(OWN, "C:\\Python\\python.exe")}
        self.table: dict[int, int] = {OWN: PARENT}
        self.hung: set[int] = set()
        self.posted: list[int] = []
        self.terminated: list[int] = []
        self.on_close = lambda hwnd: None
        self.descriptions: dict[str, str] = {}
        self.folders: dict[str, str] = {}
        self.clock = FakeClock()

    def add(self, pid, path, hwnds, **kw):
        self.procs[pid] = proc(pid, path, **kw)
        self.table.setdefault(pid, 1)
        self.windows.extend(win(h, pid) for h in hwnds)

    def kill(self, pid):
        self.procs.pop(pid, None)
        self.windows = [w for w in self.windows if w.pid != pid]


class FakeUser32:
    def __init__(self, world):
        self.w = world

    def windows(self):
        return list(self.w.windows)

    def children(self, hwnd):
        return list(self.w.children.get(hwnd, []))

    def post_close(self, hwnd):
        self.w.posted.append(hwnd)
        self.w.on_close(hwnd)

    def is_hung(self, hwnd):
        return hwnd in self.w.hung


class FakeKernel32:
    def __init__(self, world):
        self.w = world

    def current_pid(self):
        return OWN

    def process_table(self):
        return dict(self.w.table)

    def session_of(self, pid):
        return 1

    def process_info(self, pid):
        return self.w.procs.get(pid)

    def terminate(self, pid, creation):
        info = self.w.procs.get(pid)
        if info is None or info.creation != creation:
            return False
        self.w.terminated.append(pid)
        self.w.kill(pid)
        return True


class FakeVersion:
    def __init__(self, world):
        self.w = world

    def description(self, path):
        return self.w.descriptions.get(path)


class FakeShell:
    def __init__(self, world):
        self.w = world

    def known_folder(self, guid):
        return self.w.folders.get(guid)


@pytest.fixture
def world():
    return World()


def make(world, **kw):
    return WindowsRunningApps(
        FakeUser32(world),
        FakeKernel32(world),
        FakeVersion(world),
        FakeShell(world),
        clock=world.clock,
        sleep=world.clock.advance,
        system_root=ROOT,
        **kw,
    )


def only(apps):
    assert len(apps) == 1
    return apps[0]


# --- protocol / protected rules -------------------------------------------------------------


def test_implements_protocol(world):
    assert isinstance(make(world), RunningApps)
    assert isinstance(NoRunningApps(), RunningApps)


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("C:\\Windows\\System32\\svchost.exe", True),
        ("C:\\WINDOWS\\system32\\cmd.exe", True),  # under SystemRoot, not allowlisted
        ("C:\\Windows\\System32\\notepad.exe", False),  # allowlist
        ("C:\\Windows\\System32\\SnippingTool.exe", False),
        ("D:\\Tools\\Explorer.exe", True),  # denylist wherever it lives
        ("D:\\Sec\\avp.exe", True),
        ("D:\\Apps\\Telegram\\Telegram.exe", False),
        ("C:\\Windows.old\\x.exe", False),  # prefix of the root is not the root
        ("D:\\Apps\\KeePass.exe", False),
    ],
)
def test_is_protected_path(path, protected):
    assert is_protected_path(path, ROOT) is protected


def test_protected_extra_adds_names_but_never_removes():
    assert is_protected_path("D:\\Apps\\KeePass.exe", ROOT, ["keepass"])
    assert is_protected_path("C:\\Windows\\explorer.exe", ROOT, ["keepass"])


# --- listing --------------------------------------------------------------------------------


def test_groups_windows_and_processes_by_image_path(world):
    world.add(10, CHROME, [1, 2])
    world.add(11, CHROME, [3])
    world.add(20, TG, [4])
    world.descriptions[CHROME] = "Google Chrome"
    apps = {a.name: a for a in make(world).list_running()}
    chrome = apps["Google Chrome"]
    assert chrome.pids == (10, 11)
    assert chrome.windows == 3
    assert chrome.exe_path == CHROME
    assert apps["Telegram"].pids == (20,)  # no description: exe stem
    assert chrome.key.startswith(CHROME + "|10|")


def test_skips_invisible_owned_untitled_and_cloaked_windows(world):
    world.add(10, TG, [])
    world.windows += [
        win(1, 10, visible=False),
        win(2, 10, owned=True),
        win(3, 10, titled=False),
        win(4, 10, cloaked=True),
    ]
    assert make(world).list_running() == ()
    world.windows.append(win(5, 10))
    assert only(make(world).list_running()).windows == 1


def test_packaged_app_uses_core_window_process_and_aumid(world):
    world.procs[50] = proc(50, "C:\\Windows\\System32\\ApplicationFrameHost.exe")
    calc = "C:\\Program Files\\WindowsApps\\Calc\\CalculatorApp.exe"
    world.procs[51] = proc(51, calc, aumid="Calc_8we!App")
    world.windows.append(win(7, 50, "ApplicationFrameWindow"))
    world.children[7] = [win(8, 50, "Foo"), win(9, 51, "Windows.UI.Core.CoreWindow")]
    app = only(make(world).list_running())
    assert (app.pids, app.app_id, app.exe_path) == ((51,), "Calc_8we!App", calc)
    # WM_CLOSE goes to the frame, not to the CoreWindow
    world.on_close = lambda h: world.kill(51)
    assert make(world).close(app, wait_s=1) is CloseOutcome.CLOSED
    assert world.posted == [7]


def test_packaged_frame_without_core_window_is_skipped(world):
    world.procs[50] = proc(50, "C:\\Windows\\System32\\ApplicationFrameHost.exe")
    world.windows.append(win(7, 50, "ApplicationFrameWindow"))
    assert make(world).list_running() == ()


def test_protected_processes_never_listed(world):
    world.add(1, "C:\\Windows\\System32\\cmd.exe", [1])  # under SystemRoot
    world.add(2, "D:\\Elsewhere\\explorer.exe", [2])  # denylist
    world.add(3, "D:\\Apps\\KeePass.exe", [3])  # protected_extra
    world.add(4, "C:\\Windows\\System32\\notepad.exe", [4])  # allowlisted
    world.add(OWN, "C:\\Python\\python.exe", [5])  # L.U.N.A. itself
    world.add(PARENT, "C:\\Python\\pythonw.exe", [6])  # its launcher
    world.add(8, "D:\\Apps\\Elevated.exe", [8])
    world.procs.pop(8)  # cannot be opened (elevated / other user)
    names = {a.name for a in make(world, protected_extra=["keepass"]).list_running()}
    assert names == {"notepad"}


def test_other_session_is_skipped(world):
    world.add(10, TG, [1], session=2)
    assert make(world).list_running() == ()


def test_description_failure_falls_back_to_stem(world):
    world.add(10, TG, [1])
    apps = make(world)

    def boom(path):
        raise OSError

    apps._version.description = boom
    assert only(apps.list_running()).name == "Telegram"


# --- close ----------------------------------------------------------------------------------


def test_close_posts_wm_close_to_every_window_and_reports_closed(world):
    world.add(10, TG, [1, 2])
    world.on_close = lambda h: world.kill(10)
    apps = make(world)
    assert apps.close(only(apps.list_running()), wait_s=3) is CloseOutcome.CLOSED
    assert world.posted == [1, 2]


def test_close_waits_until_the_process_exits(world):
    world.add(10, TG, [1])
    apps = make(world)
    app = only(apps.list_running())
    original_sleep = apps._sleep

    def sleep(s):
        original_sleep(s)
        if world.clock() >= 0.5:
            world.kill(10)

    apps._sleep = sleep
    assert apps.close(app, wait_s=3) is CloseOutcome.CLOSED
    assert 0.5 <= world.clock() < 3


def test_close_not_running_when_gone_or_pid_reused(world):
    world.add(10, TG, [1])
    apps = make(world)
    app = only(apps.list_running())
    world.procs[10] = proc(10, TG, creation=5000)  # same pid, new process
    assert apps.close(app, wait_s=1) is CloseOutcome.NOT_RUNNING
    world.kill(10)
    assert apps.close(app, wait_s=1) is CloseOutcome.NOT_RUNNING
    assert world.posted == []


def test_close_reports_hung_window(world):
    world.add(10, TG, [1])
    world.hung.add(1)
    apps = make(world)
    assert apps.close(only(apps.list_running()), wait_s=3) is CloseOutcome.NOT_RESPONDING


def test_close_reports_save_prompt(world):
    world.add(10, TG, [1])
    world.on_close = lambda h: world.windows.append(win(99, 10, "#32770", owned=True))
    apps = make(world)
    assert apps.close(only(apps.list_running()), wait_s=3) is CloseOutcome.PROMPTED


def test_close_reports_tray_app_as_hidden(world):
    world.add(10, TG, [1])
    world.on_close = lambda h: world.windows.clear()  # window gone, process lives
    apps = make(world)
    assert apps.close(only(apps.list_running()), wait_s=3) is CloseOutcome.HIDDEN
    assert world.clock() >= 3  # waited the whole time before deciding


def test_close_that_does_nothing_is_not_responding_after_the_wait(world):
    world.add(10, TG, [1])
    apps = make(world)
    assert apps.close(only(apps.list_running()), wait_s=1) is CloseOutcome.NOT_RESPONDING
    assert world.clock() >= 1


def test_close_refuses_a_protected_key(world):
    world.add(10, TG, [1])
    apps = make(world)
    app = only(apps.list_running())
    forged = type(app)("C:\\Windows\\System32\\cmd.exe|10|1010", "x", None, None, (10,), 1)
    with pytest.raises(ProtectedApp):
        apps.close(forged, wait_s=1)
    with pytest.raises(ProtectedApp):
        apps.terminate(forged)
    assert world.posted == [] and world.terminated == []


# --- terminate ------------------------------------------------------------------------------


def test_terminate_kills_app_processes_and_same_image_children_only(world):
    world.add(10, CHROME, [1])
    world.procs[11] = proc(11, CHROME)  # helper, no window
    world.table[11] = 10
    world.procs[12] = proc(12, "D:\\Other\\helper.exe")  # child of another image
    world.table[12] = 10
    apps = make(world)
    apps.terminate(only(apps.list_running()))
    assert sorted(world.terminated) == [10, 11]
    assert 12 in world.procs


def test_terminate_refuses_a_changed_or_gone_process(world):
    world.add(10, TG, [1])
    apps = make(world)
    app = only(apps.list_running())
    world.procs[10] = proc(10, TG, creation=7000)
    with pytest.raises(ToolError):
        apps.terminate(app)
    assert world.terminated == []
    world.kill(10)
    with pytest.raises(ToolError):
        apps.terminate(app)


def test_terminate_failure_is_a_tool_error(world):
    world.add(10, TG, [1])
    apps = make(world)
    app = only(apps.list_running())
    apps._kernel32.terminate = lambda pid, creation: False
    with pytest.raises(ToolError):
        apps.terminate(app)


# --- identity -------------------------------------------------------------------------------


def test_identity_by_aumid_path_and_known_folder(world):
    from luna.platform.base import RunningApp

    guid = "{905e63b6-c1bf-494e-b29c-65b732d3d21a}"  # ProgramFiles
    world.folders[guid] = "C:\\Program Files"
    apps = make(world)
    uwp = RunningApp("k1", "Calc", "C:\\x\\Calc.exe", "Pkg_pub!App", (1,), 1)
    exe = RunningApp("k2", "Chrome", CHROME, None, (2,), 1)
    assert apps.identity_matches(uwp, AppEntry("Calc", "pkg_PUB!app"))
    assert not apps.identity_matches(uwp, AppEntry("Calc", "Other_pub!App"))
    assert apps.identity_matches(exe, AppEntry("Chrome", CHROME.lower()))
    rel = "Google\\Chrome\\Application\\chrome.exe"
    assert apps.identity_matches(exe, AppEntry("Chrome", f"{guid}\\{rel}"))
    assert not apps.identity_matches(exe, AppEntry("Chrome", f"{guid}\\Other\\chrome.exe"))
    assert not apps.identity_matches(exe, AppEntry("Chrome", "Chrome"))  # "simple" id
    assert not apps.identity_matches(exe, AppEntry("Chrome", "https://example.com"))
    world.folders.clear()
    assert not apps.identity_matches(exe, AppEntry("Chrome", f"{guid}\\{rel}"))


# --- fallback / factory ---------------------------------------------------------------------


def test_fallback_lists_nothing_and_refuses_to_act():
    from luna.platform.base import RunningApp

    no = NoRunningApps()
    app = RunningApp("k", "x", None, None, (), 0)
    assert no.list_running() == ()
    assert no.supported is False
    with pytest.raises(ToolError):
        no.close(app, wait_s=1)
    with pytest.raises(ToolError):
        no.terminate(app)
    assert not no.identity_matches(app, AppEntry("x", "x"))


def test_factory_picks_the_platform_object(monkeypatch):
    monkeypatch.setattr(factory.sys, "platform", "linux")
    assert isinstance(factory.create_running_apps(), NoRunningApps)
    monkeypatch.setattr(factory.sys, "platform", "win32")
    assert isinstance(factory.create_running_apps(["x"]), WindowsRunningApps)
