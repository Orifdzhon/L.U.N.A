# ruff: noqa: RUF001
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeBrowser, FakeConfirmer
from luna.core.errors import ConfigError, ToolError
from luna.logs.action_log import ActionLog
from luna.tools.base import SafetyLevel, Tool, ToolContext
from luna.tools.builtin.open_website import OpenWebsiteTool, default_site_aliases
from luna.tools.registry import ToolRegistry
from luna.tools.schema import validate_args

CTX = ToolContext("r1", "text", FakeConfirmer(), "open")


@pytest.fixture
def browser() -> FakeBrowser:
    return FakeBrowser()


@pytest.fixture
def tool(browser: FakeBrowser) -> OpenWebsiteTool:
    return OpenWebsiteTool(browser)


@pytest.mark.parametrize("target", ["ютуб", "YouTube", "youtube", "  Ютуб ", "YOUTUBE"])
def test_youtube_aliases(tool: OpenWebsiteTool, browser: FakeBrowser, target: str) -> None:
    res = tool.run({"target": target}, CTX)
    assert res.ok
    assert browser.urls == ["https://www.youtube.com"]


@pytest.mark.parametrize(
    ("target", "url"),
    [
        ("гитхаб", "https://github.com"),
        ("GitHub", "https://github.com"),
        ("гугл", "https://www.google.com"),
        ("хабр", "https://habr.com"),
        ("telegram web", "https://web.telegram.org"),
        ("вк", "https://vk.com"),
        ("VK", "https://vk.com"),
    ],
)
def test_other_aliases(tool: OpenWebsiteTool, browser: FakeBrowser, target: str, url: str) -> None:
    assert tool.run({"target": target}, CTX).ok
    assert browser.urls == [url]


@pytest.mark.parametrize(
    ("target", "url"),
    [
        ("habr.com", "https://habr.com"),
        ("HABR.com", "https://HABR.com"),
        ("www.example.org/path", "https://www.example.org/path"),
        ("example.com:8080", "https://example.com:8080"),
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("http://example.com", "http://example.com"),
        ("HTTPS://Example.com/x", "HTTPS://Example.com/x"),
        ("кремль.рф", "https://кремль.рф"),
    ],
)
def test_domains_and_urls(
    tool: OpenWebsiteTool, browser: FakeBrowser, target: str, url: str
) -> None:
    res = tool.run({"target": target}, CTX)
    assert res.ok, res
    assert browser.urls == [url]


@pytest.mark.parametrize(
    "target",
    [
        "file:///C:/Windows/System32",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,hi",
        "C:\\Windows",
        "C:/Windows",
        "\\\\server\\share",
        "//evil.com",
        "ftp://x",
        "ftp://example.com/file",
        "mailto:a@b.com",
        "about:blank",
        "https://user:pw@example.com",
        "user:pw@example.com",
        "https://example.com/a b",
        "habr.com\x00",
        "https:///x",
    ],
)
def test_unsafe_targets_rejected(tool: OpenWebsiteTool, browser: FakeBrowser, target: str) -> None:
    res = tool.run({"target": target}, CTX)
    assert not res.ok
    assert res.data["reason"] == "unsafe_target"
    assert browser.urls == []


@pytest.mark.parametrize(
    "target",
    ["неизвестный сайт", "foo", "localhost", "192.168.0.1", "hello world", "a.b", "-x.com"],
)
def test_no_match(tool: OpenWebsiteTool, browser: FakeBrowser, target: str) -> None:
    res = tool.run({"target": target}, CTX)
    assert not res.ok
    assert res.data == {"reason": "no_match"}
    assert browser.urls == []


def test_empty_target_opens_home(browser: FakeBrowser) -> None:
    t = OpenWebsiteTool(browser, home_url="https://start.example")
    assert t.run({}, CTX).ok
    assert t.run({"target": "  "}, CTX).ok
    assert browser.urls == ["https://start.example"] * 2


def test_default_home_is_http(tool: OpenWebsiteTool, browser: FakeBrowser) -> None:
    assert tool.run({"target": ""}, CTX).ok
    assert browser.urls[0].startswith("https://")


def test_non_string_target(tool: OpenWebsiteTool, browser: FakeBrowser) -> None:
    assert not tool.run({"target": 5}, CTX).ok
    assert browser.urls == []


def test_user_overrides_win(browser: FakeBrowser) -> None:
    t = OpenWebsiteTool(browser, {"Ютуб": "https://yewtu.example", "моя вики": "https://wiki.me"})
    t.run({"target": "ютуб"}, CTX)
    t.run({"target": "Моя  вики"}, CTX)
    assert browser.urls == ["https://yewtu.example", "https://wiki.me"]


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "file:///x", "ftp://x", "not a url"])
def test_unsafe_override_rejected_at_construction(browser: FakeBrowser, bad: str) -> None:
    with pytest.raises(ConfigError):
        OpenWebsiteTool(browser, {"evil": bad})


def test_bad_home_url(browser: FakeBrowser) -> None:
    with pytest.raises(ConfigError):
        OpenWebsiteTool(browser, home_url="about:blank")


def test_shipped_aliases_are_valid_and_cover_issue() -> None:
    aliases = default_site_aliases()
    assert len(aliases) >= 20
    assert all(u.startswith("https://") for u in aliases.values())
    for key in ("ютуб", "youtube", "гитхаб", "github", "google", "гугл", "habr", "вк", "vk"):
        assert key in aliases


def test_opener_failures_become_results() -> None:
    class Failing:
        def open_url(self, url: str) -> None:
            raise ToolError("no browser")

    res = OpenWebsiteTool(Failing()).run({"target": "habr.com"}, CTX)
    assert not res.ok
    assert res.data["reason"] == "open_failed"


def test_summary_has_host_only(tool: OpenWebsiteTool) -> None:
    res = tool.run({"target": "https://example.com/path?token=secret"}, CTX)
    assert res.summary == "Opened example.com"
    assert "secret" not in str(res.data)


def test_is_safe_tool(tool: OpenWebsiteTool) -> None:
    assert isinstance(tool, Tool)
    assert tool.safety is SafetyLevel.SAFE


def test_schema_registers_and_validates(tool: OpenWebsiteTool, tmp_path: Path) -> None:
    registry = ToolRegistry(ActionLog(tmp_path / "a.jsonl"))
    registry.register(tool)
    assert [s.name for s in registry.schemas()] == ["open_website"]
    schema: dict[str, Any] = tool.parameters
    assert validate_args({"target": "ютуб"}, schema) == []
    assert validate_args({}, schema) == []
    assert validate_args({"target": 1}, schema)
    assert validate_args({"target": "x", "extra": 1}, schema)
    assert validate_args({"target": "x" * 3000}, schema)
