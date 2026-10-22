# ruff: noqa: RUF001
import webbrowser

import pytest

from luna.core.errors import ToolError
from luna.platform.base import BrowserOpener
from luna.platform.browser import WebBrowserOpener, validate_web_url


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake_open(url: str, *a: object, **k: object) -> bool:
        seen.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    return seen


def test_satisfies_protocol() -> None:
    assert isinstance(WebBrowserOpener(), BrowserOpener)


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com:8080/a?b=1#c"])
def test_opens_web_urls(url: str, calls: list[str]) -> None:
    WebBrowserOpener().open_url(url)
    assert calls == [url]


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/System32",
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "ftp://example.com",
        "about:blank",
        "C:\\Windows",
        "\\\\server\\share",
        "https://user:pw@example.com",
        "https://example.com/a b",
        "https://example.com/\n",
        "https:///nohost",
        "https://example.com:0/",
        "https://example.com:99999/",
        'https://x.com/"-osint',
        "https://x.com/<a>",
        "https://x.com/a^b",
        "https://x.com/a`b",
        "https://x.com/a|b",
        "https://x.com/{a}",
        "https://аpple.com",
        "https://ｅxample.com",
        "",
        "x" * 3000,
    ],
)
def test_rejects_unsafe_urls(url: str, calls: list[str]) -> None:
    with pytest.raises(ValueError):
        WebBrowserOpener().open_url(url)
    assert calls == []


def test_browser_returning_false_is_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)
    with pytest.raises(ToolError):
        WebBrowserOpener().open_url("https://example.com")


def test_os_error_is_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> bool:
        raise OSError("no shell")

    monkeypatch.setattr(webbrowser, "open", boom)
    with pytest.raises(ToolError):
        WebBrowserOpener().open_url("https://example.com")


def test_validate_returns_url_unchanged() -> None:
    assert validate_web_url("https://example.com/a?b=1") == "https://example.com/a?b=1"


@pytest.mark.parametrize(
    ("given", "opened"),
    [
        ("HTTPS://Example.COM/Path?Q=1", "https://example.com/Path?Q=1"),
        ("https://яндекс.рф/", "https://xn--d1acpjx3f.xn--p1ai/"),
        ("https://x.com/?a=1&b=2", "https://x.com/?a=1&b=2"),
        ("http://[::1]:8080/", "http://[::1]:8080/"),
    ],
)
def test_opens_normalized_url(given: str, opened: str, calls: list[str]) -> None:
    WebBrowserOpener().open_url(given)
    assert calls == [opened]
