from pathlib import Path
from typing import Any

import pytest

from fakes import FakeBrowser, FakeConfirmer
from luna.llm.base import ToolCall
from luna.logs.action_log import ActionLog
from luna.tools.base import ToolContext
from luna.tools.builtin.open_website import OpenWebsiteTool
from luna.tools.registry import ToolRegistry
from luna.tools.schema import validate_args

CTX = ToolContext("r1", "text", FakeConfirmer(), "open")


@pytest.fixture
def browser() -> FakeBrowser:
    return FakeBrowser()


@pytest.fixture
def tool(browser: FakeBrowser) -> OpenWebsiteTool:
    return OpenWebsiteTool(browser)


@pytest.mark.parametrize(
    ("target", "query", "url"),
    [
        (
            "ютуб",
            "lofi hip hop radio",
            "https://www.youtube.com/results?search_query=lofi+hip+hop+radio",
        ),
        (
            "youtube",
            "кино про космос",
            "https://www.youtube.com/results?search_query=%D0%BA%D0%B8%D0%BD%D0%BE+%D0%BF%D1%80%D0%BE+%D0%BA%D0%BE%D1%81%D0%BC%D0%BE%D1%81",
        ),
        ("google", "a&b=c", "https://www.google.com/search?q=a%26b%3Dc"),
        ("яндекс", "погода", "https://yandex.ru/search/?text=%D0%BF%D0%BE%D0%B3%D0%BE%D0%B4%D0%B0"),
        ("яндекс музыка", "Кино", "https://music.yandex.ru/search?text=%D0%9A%D0%B8%D0%BD%D0%BE"),
        (
            "вк",
            "ночь",
            "https://vk.com/search?c%5Bq%5D=%D0%BD%D0%BE%D1%87%D1%8C&c%5Bsection%5D=auto",
        ),
        ("вк видео", "кот", "https://vkvideo.ru/search?q=%D0%BA%D0%BE%D1%82"),
        ("rutube", "cats", "https://rutube.ru/search/?query=cats"),
        ("spotify", "daft punk", "https://open.spotify.com/search/daft%20punk"),
        ("soundcloud", "lofi", "https://soundcloud.com/search?q=lofi"),
        ("github", "luna assistant", "https://github.com/search?q=luna+assistant"),
        ("озон", "чайник", "https://www.ozon.ru/search/?text=%D1%87%D0%B0%D0%B9%D0%BD%D0%B8%D0%BA"),
        ("wildberries", "socks", "https://www.wildberries.ru/catalog/0/search.aspx?search=socks"),
        ("avito", "bike", "https://www.avito.ru/all?q=bike"),
        ("twitch", "chess", "https://www.twitch.tv/search?term=chess"),
        ("tiktok", "dance", "https://www.tiktok.com/search?q=dance"),
        ("reddit", "python", "https://www.reddit.com/search/?q=python"),
        ("habr", "docker", "https://habr.com/ru/search/?q=docker"),
        ("stackoverflow", "asyncio", "https://stackoverflow.com/search?q=asyncio"),
        (
            "wikipedia",
            "Луна",
            "https://ru.wikipedia.org/w/index.php?search=%D0%9B%D1%83%D0%BD%D0%B0",
        ),
        ("wikipedia", "Moon", "https://en.wikipedia.org/w/index.php?search=Moon"),
        ("https://de.wikipedia.org", "Mond", "https://de.wikipedia.org/w/index.php?search=Mond"),
        ("youtube.com", "x", "https://www.youtube.com/results?search_query=x"),
    ],
)
def test_site_search_url(
    tool: OpenWebsiteTool, browser: FakeBrowser, target: str, query: str, url: str
) -> None:
    res = tool.run({"target": target, "query": query}, CTX)
    assert res.ok, res
    assert browser.urls == [url]
    assert query not in res.summary
    assert res.data["search"] is True


def test_unknown_site_falls_back_to_site_search(
    tool: OpenWebsiteTool, browser: FakeBrowser
) -> None:
    assert tool.run({"target": "example.org", "query": "кот в мешке"}, CTX).ok
    assert browser.urls == [
        "https://www.google.com/search?q=site%3Aexample.org+%D0%BA%D0%BE%D1%82+%D0%B2+%D0%BC%D0%B5%D1%88%D0%BA%D0%B5"
    ]


def test_query_without_target_uses_default_engine(
    tool: OpenWebsiteTool, browser: FakeBrowser
) -> None:
    assert tool.run({"query": "a b"}, CTX).ok
    assert browser.urls == ["https://www.google.com/search?q=a+b"]


def test_empty_query_just_opens_site(tool: OpenWebsiteTool, browser: FakeBrowser) -> None:
    res = tool.run({"target": "ютуб", "query": "  "}, CTX)
    assert res.ok and browser.urls == ["https://www.youtube.com"]
    assert "search" not in res.data


def test_query_cannot_inject_url_parts(tool: OpenWebsiteTool, browser: FakeBrowser) -> None:
    assert tool.run({"target": "youtube", "query": "x#frag&evil=1 /../?"}, CTX).ok
    url = browser.urls[0]
    assert url.startswith("https://www.youtube.com/results?search_query=")
    assert "#" not in url and "&" not in url and " " not in url


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"target": "youtube", "query": 5}, "invalid_query"),
        ({"target": "youtube", "query": "a\x00b"}, "invalid_query"),
        ({"target": "youtube", "query": "a\u202eb"}, "invalid_query"),
        ({"target": "youtube", "query": "x" * 201}, "invalid_query"),
        ({"target": "file:///C:/x", "query": "q"}, "unsafe_target"),
        ({"target": "javascript:alert(1)", "query": "q"}, "unsafe_target"),
        ({"target": "http://127.0.0.1/", "query": "q"}, "unsafe_target"),
        ({"target": "calc.exe", "query": "q"}, "no_match"),
    ],
)
def test_search_unsafe_inputs_refused(
    tool: OpenWebsiteTool, browser: FakeBrowser, args: dict[str, Any], reason: str
) -> None:
    res = tool.run(args, CTX)
    assert not res.ok and res.data["reason"] == reason
    assert browser.urls == []


def test_query_schema_is_strict(tool: OpenWebsiteTool) -> None:
    schema: dict[str, Any] = tool.parameters
    assert validate_args({"target": "ютуб", "query": "lofi"}, schema) == []
    assert validate_args({"query": 1}, schema)
    assert validate_args({"query": "x" * 201}, schema)
    assert validate_args({"target": "x", "query": "y", "extra": 1}, schema)
    assert "query" in tool.description


def test_action_record_has_query_but_no_built_url(tmp_path: Path, browser: FakeBrowser) -> None:
    log = ActionLog(tmp_path / "a.jsonl")
    registry = ToolRegistry(log)
    registry.register(OpenWebsiteTool(browser))
    ctx = ToolContext("r1", "text", FakeConfirmer(), "найди котиков на ютубе")
    call = ToolCall("c1", "open_website", {"target": "ютуб", "query": "котики"})
    assert registry.execute(call, ctx).ok
    text = log.current_path().read_text(encoding="utf-8")
    assert "котики" in text  # the user's own words, already stored in the record's "text"
    assert "search_query" not in text and "youtube.com/results" not in text
