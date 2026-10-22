import json
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from luna.core.errors import ProviderUnavailable, ToolError
from luna.platform.base import AppCatalog, AppEntry, AppLauncher
from luna.platform.windows.apps import (
    WindowsAppCatalog,
    WindowsAppLauncher,
    is_safe_app_id,
    parse_start_apps,
    powershell_argv,
)

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "get_startapps.json"


def completed(stdout: bytes = b"", returncode: int = 0) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


class FakeRunner:
    def __init__(self, *results: "subprocess.CompletedProcess[bytes] | Exception") -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: Sequence[str], timeout_s: float
    ) -> "subprocess.CompletedProcess[bytes]":
        self.calls.append((list(argv), timeout_s))
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, Exception):
            raise item
        return item


def fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_fixture_parses_with_cyrillic_and_aumid() -> None:
    apps = parse_start_apps(fixture_bytes())
    assert len(apps) == 15
    by_name = {a.name: a.app_id for a in apps}
    assert by_name["Калькулятор"] == "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    assert by_name["Блокнот"] == "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\notepad.exe"
    assert by_name["Telegram"] == "Telegram.TelegramDesktop"
    assert by_name["Google Chrome"] == "Chrome"


def test_single_object_instead_of_array() -> None:
    apps = parse_start_apps(b'{"Name": "Telegram", "AppID": "Telegram.TelegramDesktop"}')
    assert apps == (AppEntry("Telegram", "Telegram.TelegramDesktop"),)


@pytest.mark.parametrize("raw", [b"", b"  \r\n", b"null", b"[]", "", b"\xef\xbb\xbf"])
def test_empty_output(raw: bytes | str) -> None:
    assert parse_start_apps(raw) == ()


def test_bom_and_utf8_cyrillic_bytes() -> None:
    raw = '\ufeff[{"Name": "Блокнот", "AppID": "Notepad"}]'.encode()
    assert parse_start_apps(raw) == (AppEntry("Блокнот", "Notepad"),)


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2", b"42", b'"text"', b"\xff\xfe\x00bad"])
def test_malformed_output_raises_value_error(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_start_apps(raw)


def test_malformed_entries_are_dropped_and_duplicates_collapsed() -> None:
    raw = json.dumps(
        [
            {"Name": "Good", "AppID": "Good.App"},
            {"Name": "Good again", "AppID": "Good.App"},
            {"Name": "", "AppID": "Empty.Name"},
            {"Name": "No id"},
            {"Name": 5, "AppID": "X"},
            {"Name": "Evil", "AppID": 'a" & calc & "'},
            "string",
            None,
        ]
    )
    assert parse_start_apps(raw) == (AppEntry("Good", "Good.App"),)


@pytest.mark.parametrize(
    "app_id",
    [
        "Chrome",
        "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
        "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\notepad.exe",
        "{7C5A}\\Microsoft Office\\root\\Office16\\WINWORD.EXE",
        "Приложение.Тест",
    ],
)
def test_safe_app_ids(app_id: str) -> None:
    assert is_safe_app_id(app_id)


@pytest.mark.parametrize(
    "app_id",
    [
        "",
        " Chrome",
        "Chrome ",
        "a\nb",
        "a\x00b",
        'a"b',
        "a&b",
        "a|b",
        "a<b",
        "a>b",
        "a^b",
        "a;b",
        "a`b",
        "a$b",
        "%PATH%",
        "a/b",
        "..\\evil.exe",
        "x\\..\\evil.exe",
        "shell:Startup",
        "SHELL:AppsFolder",
        "a" * 600,
        None,
        5,
    ],
)
def test_unsafe_app_ids(app_id: object) -> None:
    assert not is_safe_app_id(app_id)


def test_powershell_argv_is_fixed_and_utf8() -> None:
    argv = powershell_argv()
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-StartApps | ConvertTo-Json" in argv[4]
    assert "UTF8" in argv[4]


def test_protocols_satisfied(tmp_path: Path) -> None:
    catalog = WindowsAppCatalog(tmp_path, FakeRunner(completed(b"[]")))
    assert isinstance(catalog, AppCatalog)
    assert isinstance(WindowsAppLauncher(catalog, startfile=lambda p: None), AppLauncher)


def test_list_apps_scans_once_then_serves_snapshot(tmp_path: Path) -> None:
    runner = FakeRunner(completed(fixture_bytes()))
    catalog = WindowsAppCatalog(tmp_path, runner)
    first = catalog.list_apps()
    assert len(first) == 15
    assert catalog.list_apps() is first
    assert len(runner.calls) == 1
    assert runner.calls[0][1] > 0  # timeout is passed


def test_cache_written_and_reused_by_second_instance(tmp_path: Path) -> None:
    runner = FakeRunner(completed(fixture_bytes()))
    first = WindowsAppCatalog(tmp_path, runner).list_apps()
    cache = tmp_path / "apps.json"
    assert cache.exists()
    assert "Калькулятор" in cache.read_text(encoding="utf-8")  # stored unescaped
    runner2 = FakeRunner(completed(b"[]"))
    second = WindowsAppCatalog(tmp_path, runner2).list_apps()
    assert second == first
    assert runner2.calls == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_refresh_replaces_snapshot_and_cache(tmp_path: Path) -> None:
    one = b'{"Name": "One", "AppID": "One.App"}'
    runner = FakeRunner(completed(one), completed(fixture_bytes()))
    catalog = WindowsAppCatalog(tmp_path, runner)
    old = catalog.list_apps()
    assert old == (AppEntry("One", "One.App"),)
    catalog.refresh()
    assert len(catalog.list_apps()) == 15
    assert old == (AppEntry("One", "One.App"),)  # old snapshot untouched (immutable)
    assert len(WindowsAppCatalog(tmp_path, FakeRunner(completed())).list_apps()) == 15


def test_refresh_is_atomic_for_concurrent_readers(tmp_path: Path) -> None:
    gate = threading.Event()
    started = threading.Event()

    def runner(argv: Sequence[str], timeout_s: float) -> "subprocess.CompletedProcess[bytes]":
        if len(calls) == 1:
            calls.append(1)
            started.set()
            gate.wait(5)
            return completed(fixture_bytes())
        calls.append(0)
        return completed(b'{"Name": "One", "AppID": "One.App"}')

    calls: list[int] = []
    catalog = WindowsAppCatalog(tmp_path, runner)
    catalog.list_apps()  # first scan: one entry
    t = threading.Thread(target=catalog.refresh)
    t.start()
    assert started.wait(5)
    assert catalog.list_apps() == (AppEntry("One", "One.App"),)  # old snapshot while scanning
    gate.set()
    t.join(5)
    assert len(catalog.list_apps()) == 15


@pytest.mark.parametrize(
    "failure",
    [
        completed(b"", returncode=1),
        subprocess.TimeoutExpired(cmd="powershell", timeout=1),
        FileNotFoundError("powershell.exe"),
        completed(b"{broken"),
    ],
)
def test_failure_without_cache_raises_provider_unavailable(
    tmp_path: Path, failure: "subprocess.CompletedProcess[bytes] | Exception"
) -> None:
    catalog = WindowsAppCatalog(tmp_path, FakeRunner(failure))
    with pytest.raises(ProviderUnavailable):
        catalog.list_apps()
    with pytest.raises(ProviderUnavailable):
        catalog.refresh()


@pytest.mark.parametrize(
    "failure",
    [
        completed(b"", returncode=1),
        subprocess.TimeoutExpired(cmd="powershell", timeout=1),
        completed(b"{broken"),
    ],
)
def test_failure_with_snapshot_keeps_cached_list(
    tmp_path: Path, failure: "subprocess.CompletedProcess[bytes] | Exception"
) -> None:
    catalog = WindowsAppCatalog(tmp_path, FakeRunner(completed(fixture_bytes()), failure))
    before = catalog.list_apps()
    catalog.refresh()  # no exception: cached list stays
    assert catalog.list_apps() == before


def test_failure_with_only_disk_cache_serves_cache(tmp_path: Path) -> None:
    WindowsAppCatalog(tmp_path, FakeRunner(completed(fixture_bytes()))).list_apps()
    catalog = WindowsAppCatalog(tmp_path, FakeRunner(completed(b"", returncode=1)))
    catalog.refresh()
    assert len(catalog.list_apps()) == 15


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"version": 99, "apps": []}',
        '{"version": 1}',
        '{"version": 1, "apps": 5}',
    ],
)
def test_corrupt_cache_is_ignored(tmp_path: Path, content: str) -> None:
    (tmp_path / "apps.json").write_text(content, encoding="utf-8")
    runner = FakeRunner(completed(fixture_bytes()))
    assert len(WindowsAppCatalog(tmp_path, runner).list_apps()) == 15
    assert len(runner.calls) == 1


def test_tampered_cache_entries_are_validated(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "apps": [{"name": "Ok", "app_id": "Ok.App"}, {"name": "Bad", "app_id": "..\\x"}],
    }
    (tmp_path / "apps.json").write_text(json.dumps(payload), encoding="utf-8")
    apps = WindowsAppCatalog(tmp_path, FakeRunner(completed())).list_apps()
    assert apps == (AppEntry("Ok", "Ok.App"),)


def test_unwritable_cache_dir_is_not_fatal(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    catalog = WindowsAppCatalog(blocker / "sub", FakeRunner(completed(fixture_bytes())))
    assert len(catalog.list_apps()) == 15


# -- launcher ---------------------------------------------------------------------------------


def make_launcher(tmp_path: Path) -> tuple[WindowsAppLauncher, list[str], tuple[AppEntry, ...]]:
    started: list[str] = []
    catalog = WindowsAppCatalog(tmp_path, FakeRunner(completed(fixture_bytes())))
    return (
        WindowsAppLauncher(catalog, startfile=started.append),
        started,
        catalog.list_apps(),
    )


def test_launch_builds_apps_folder_path(tmp_path: Path) -> None:
    launcher, started, apps = make_launcher(tmp_path)
    by_name = {a.name: a for a in apps}
    launcher.launch(by_name["Telegram"])
    launcher.launch(by_name["Калькулятор"])
    launcher.launch(by_name["Блокнот"])
    assert started == [
        "shell:AppsFolder\\Telegram.TelegramDesktop",
        "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
        "shell:AppsFolder\\{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\notepad.exe",
    ]


@pytest.mark.parametrize(
    "entry",
    [
        AppEntry("Evil", "evil.exe"),
        AppEntry("Telegram", "Telegram.Other"),  # known name, unknown id
        AppEntry("Evil", "C:\\Windows\\System32\\cmd.exe"),
        AppEntry("Evil", "shell:Startup"),
    ],
)
def test_launcher_rejects_entries_not_in_catalog(tmp_path: Path, entry: AppEntry) -> None:
    launcher, started, _ = make_launcher(tmp_path)
    with pytest.raises(ToolError):
        launcher.launch(entry)
    assert started == []


def test_launcher_rejects_non_entry(tmp_path: Path) -> None:
    launcher, started, _ = make_launcher(tmp_path)
    with pytest.raises(ToolError):
        launcher.launch("Telegram.TelegramDesktop")  # type: ignore[arg-type]
    assert started == []


def test_launcher_rejects_unsafe_id_even_if_catalog_has_it(tmp_path: Path) -> None:
    from fakes import FakeAppCatalog

    evil = AppEntry("Evil", "a&calc")
    started: list[str] = []
    launcher = WindowsAppLauncher(FakeAppCatalog([evil]), startfile=started.append)
    with pytest.raises(ToolError):
        launcher.launch(evil)
    assert started == []


def test_launcher_wraps_os_errors(tmp_path: Path) -> None:
    catalog = WindowsAppCatalog(tmp_path, FakeRunner(completed(fixture_bytes())))

    def boom(path: str) -> None:
        raise OSError("no association")

    launcher = WindowsAppLauncher(catalog, startfile=boom)
    with pytest.raises(ToolError, match="cannot launch"):
        launcher.launch(catalog.list_apps()[0])


@pytest.mark.live
@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
def test_real_get_startapps_lists_apps(tmp_path: Path) -> None:
    apps = WindowsAppCatalog(tmp_path).list_apps()
    assert len(apps) > 0
    assert all(is_safe_app_id(a.app_id) and a.name for a in apps)
