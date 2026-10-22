from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeAppCatalog, FakeClock, FakeConfirmer, FakeLauncher
from luna.core.errors import ProviderUnavailable, ToolError
from luna.logs.action_log import ActionLog
from luna.platform.base import AppEntry
from luna.tools.base import SafetyLevel, Tool, ToolContext
from luna.tools.builtin.open_app import OpenAppTool
from luna.tools.registry import ToolRegistry
from luna.tools.schema import validate_args

CTX = ToolContext("r1", "text", FakeConfirmer(), "open")
APPS = [
    AppEntry("Telegram", "Telegram.TelegramDesktop"),
    AppEntry("Google Chrome", "Chrome"),
    AppEntry("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    AppEntry("Блокнот", "notepad"),
    AppEntry("Microsoft Edge", "MSEdge"),
]


@pytest.fixture
def catalog() -> FakeAppCatalog:
    return FakeAppCatalog(APPS)


@pytest.fixture
def launcher(catalog: FakeAppCatalog) -> FakeLauncher:
    return FakeLauncher(catalog)


@pytest.fixture
def tool(catalog: FakeAppCatalog, launcher: FakeLauncher) -> OpenAppTool:
    return OpenAppTool(catalog, launcher)


@pytest.mark.parametrize(
    ("name", "app"),
    [
        ("telegram", "Telegram"),
        ("телеграм", "Telegram"),
        ("телега", "Telegram"),
        ("хром", "Google Chrome"),
        ("chrome", "Google Chrome"),
        ("калькулятор", "Калькулятор"),
        ("calculator", "Калькулятор"),
        ("блокнот", "Блокнот"),
        ("notepad", "Блокнот"),
    ],
)
def test_launches_catalog_entry(
    tool: OpenAppTool, launcher: FakeLauncher, catalog: FakeAppCatalog, name: str, app: str
) -> None:
    res = tool.run({"name": name}, CTX)
    assert res.ok
    assert [a.name for a in launcher.launched] == [app]
    assert launcher.launched[0] in catalog.list_apps()
    assert catalog.refresh_count == 0


def test_no_match_refreshes_once_and_launches_nothing(
    tool: OpenAppTool, launcher: FakeLauncher, catalog: FakeAppCatalog
) -> None:
    res = tool.run({"name": "фотошоп"}, CTX)
    assert not res.ok
    assert res.data == {"reason": "no_match"}
    assert launcher.launched == []
    assert catalog.refresh_count == 1


def test_refresh_picks_up_newly_installed_app(
    tool: OpenAppTool, launcher: FakeLauncher, catalog: FakeAppCatalog
) -> None:
    class Refreshing(FakeAppCatalog):
        def refresh(self) -> None:
            super().refresh()
            self.apps.append(AppEntry("Discord", "Discord"))

    cat = Refreshing(APPS)
    t = OpenAppTool(cat, FakeLauncher(cat))
    assert t.run({"name": "discord"}, CTX).ok
    assert cat.refresh_count == 1


def test_ambiguous_lists_candidates_and_launches_nothing(launcher: FakeLauncher) -> None:
    cat = FakeAppCatalog([AppEntry("Visual Studio 2019", "a"), AppEntry("Visual Studio 2022", "b")])
    launcher = FakeLauncher(cat)
    res = OpenAppTool(cat, launcher).run({"name": "visual studio"}, CTX)
    assert not res.ok
    assert res.data["reason"] == "ambiguous"
    assert sorted(res.data["candidates"]) == ["Visual Studio 2019", "Visual Studio 2022"]
    assert "Which one" in res.summary
    assert launcher.launched == []
    assert cat.refresh_count == 0


@pytest.mark.parametrize("name", ["C:\\Windows\\System32\\cmd.exe", "cmd /c del *", "cmd.exe"])
def test_raw_paths_and_commands_never_launch(
    catalog: FakeAppCatalog, launcher: FakeLauncher, name: str
) -> None:
    catalog.apps.append(AppEntry("Command Prompt", "cmd"))
    res = OpenAppTool(catalog, launcher).run({"name": name}, CTX)
    assert not res.ok
    assert res.data["reason"] == "no_match"
    assert launcher.launched == []


@pytest.mark.parametrize("args", [{}, {"name": ""}, {"name": "  "}, {"name": 5}, {"name": None}])
def test_invalid_name(tool: OpenAppTool, launcher: FakeLauncher, args: dict[str, Any]) -> None:
    res = tool.run(args, CTX)
    assert not res.ok
    assert res.data == {"reason": "invalid_name"}
    assert launcher.launched == []


class BrokenCatalog:
    def list_apps(self) -> Sequence[AppEntry]:
        raise ProviderUnavailable("no PowerShell")

    def refresh(self) -> None:
        raise ProviderUnavailable("no PowerShell")


def test_catalog_unavailable_is_graceful() -> None:
    launcher = FakeLauncher()
    res = OpenAppTool(BrokenCatalog(), launcher).run({"name": "telegram"}, CTX)
    assert not res.ok
    assert res.data == {"reason": "catalog_unavailable"}
    assert launcher.launched == []


def test_refresh_failure_on_miss_is_no_match() -> None:
    class RefreshFails(FakeAppCatalog):
        def refresh(self) -> None:
            raise ProviderUnavailable("busy")

    cat = RefreshFails(APPS)
    res = OpenAppTool(cat, FakeLauncher(cat)).run({"name": "фотошоп"}, CTX)
    assert res.data == {"reason": "no_match"}


def test_launch_failure_is_reported(catalog: FakeAppCatalog) -> None:
    class Failing:
        def launch(self, app: AppEntry) -> None:
            raise ToolError("boom")

    res = OpenAppTool(catalog, Failing()).run({"name": "telegram"}, CTX)
    assert not res.ok
    assert res.data == {"reason": "open_failed"}


def test_user_aliases_and_thresholds(catalog: FakeAppCatalog, launcher: FakeLauncher) -> None:
    t = OpenAppTool(catalog, launcher, {"мессенджер": "Telegram"}, threshold=100, margin=10)
    assert t.run({"name": "мессенджер"}, CTX).ok
    assert launcher.launched[-1].name == "Telegram"
    assert not t.run({"name": "telegrm"}, CTX).ok


def test_is_safe_tool(tool: OpenAppTool) -> None:
    assert isinstance(tool, Tool)
    assert tool.safety is SafetyLevel.SAFE


def test_schema_registers_and_validates(tool: OpenAppTool, tmp_path: Path) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    registry.register(tool)
    assert [s.name for s in registry.schemas()] == ["open_app"]
    schema: dict[str, Any] = tool.parameters
    assert validate_args({"name": "хром"}, schema) == []
    assert validate_args({}, schema)
    assert validate_args({"name": 1}, schema)
    assert validate_args({"name": "x", "extra": 1}, schema)
    assert validate_args({"name": "x" * 500}, schema)


def test_refresh_on_miss_is_rate_limited(catalog: FakeAppCatalog, launcher: FakeLauncher) -> None:
    clock = FakeClock()
    t = OpenAppTool(catalog, launcher, clock=clock)
    assert t.run({"name": "фотошоп"}, CTX).data == {"reason": "no_match"}
    assert t.run({"name": "gimp"}, CTX).data == {"reason": "no_match"}
    assert catalog.refresh_count == 1
    clock.advance(301)
    assert t.run({"name": "gimp"}, CTX).data == {"reason": "no_match"}
    assert catalog.refresh_count == 2


@pytest.mark.parametrize("name", ["закрой телеграм", "удали телеграм", "хром и телеграм"])
def test_other_intents_launch_nothing(
    tool: OpenAppTool, launcher: FakeLauncher, catalog: FakeAppCatalog, name: str
) -> None:
    res = tool.run({"name": name}, CTX)
    assert not res.ok
    assert res.data == {"reason": "unsupported_request"}
    assert launcher.launched == []
    assert catalog.refresh_count == 0


@pytest.mark.parametrize("name", ["чат", "окно", "блок", "tel", "музыка"])
def test_partial_names_launch_nothing(tool: OpenAppTool, launcher: FakeLauncher, name: str) -> None:
    assert not tool.run({"name": name}, CTX).ok
    assert launcher.launched == []


@pytest.mark.parametrize("name", ["Notepad++", "Battle.net"])
def test_names_with_punctuation(name: str) -> None:
    cat = FakeAppCatalog([AppEntry("Notepad++", "n"), AppEntry("Battle.net", "b")])
    launcher = FakeLauncher(cat)
    assert OpenAppTool(cat, launcher).run({"name": name}, CTX).ok
    assert launcher.launched[0].name == name
