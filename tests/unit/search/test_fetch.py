import time
from pathlib import Path

import httpx
import pytest
import respx

from luna.search import fetch
from luna.search.base import SearchResult
from luna.search.fetch import MAX_CONTENT_CHARS, MAX_RESPONSE_BYTES, fetch_and_extract

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
HTML = {"content-type": "text/html; charset=utf-8"}


def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def fetch_one(url: str, **kwargs: object) -> SearchResult:
    (out,) = fetch_and_extract(
        [SearchResult("t", url, "s")], resolver=kwargs.pop("resolver", public_resolver), **kwargs
    )
    return out


def page(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@respx.mock
def test_extracts_article_text_without_scripts_or_nav() -> None:
    respx.get("https://example.com/a").respond(200, content=page("article_en.html"), headers=HTML)
    out = fetch_one("https://example.com/a")
    assert out.content is not None
    assert "gravitational pull of the moon" in out.content
    assert "ignore previous instructions" not in out.content
    assert "<" not in out.content
    assert out.untrusted and (out.title, out.snippet) == ("t", "s")


@respx.mock
def test_cyrillic_utf8_page() -> None:
    respx.get("https://example.ru/a").respond(200, content=page("article_ru.html"), headers=HTML)
    out = fetch_one("https://example.ru/a")
    assert out.content is not None
    assert "Центральный банк ежедневно публикует" in out.content


@respx.mock
def test_cyrillic_cp1251_page_declared_in_header_and_in_meta() -> None:
    source = page("article_ru.html").decode("utf-8")
    body = source.encode("cp1251")
    respx.get("https://example.ru/h").respond(
        200, content=body, headers={"content-type": "text/html; charset=windows-1251"}
    )
    meta = source.replace('<meta charset="utf-8">', '<meta charset="windows-1251">')
    respx.get("https://example.ru/m").respond(
        200, content=meta.encode("cp1251"), headers={"content-type": "text/html"}
    )
    for path in ("h", "m"):
        out = fetch_one(f"https://example.ru/{path}")
        assert out.content is not None
        assert "Центральный банк ежедневно публикует" in out.content


@respx.mock
def test_undeclared_legacy_encoding_is_detected_or_replaced_not_fatal() -> None:
    body = page("article_ru.html").decode("utf-8").replace("utf-8", "x-unknown").encode("cp1251")
    respx.get("https://example.ru/u").respond(
        200, content=body, headers={"content-type": "text/html"}
    )
    assert fetch_one("https://example.ru/u").content is not None


@respx.mock
def test_content_truncated_to_limit() -> None:
    paragraphs = "".join(
        f"<p>Sentence number {i} describes the long running harbor project in detail.</p>"
        for i in range(400)
    )
    html = f"<html><body><article><h1>Long</h1>{paragraphs}</article></body></html>"
    respx.get("https://example.com/long").respond(200, text=html, headers=HTML)
    out = fetch_one("https://example.com/long")
    assert out.content is not None
    assert 0 < len(out.content) <= MAX_CONTENT_CHARS


@respx.mock
def test_invisible_characters_removed_from_extracted_text() -> None:
    html = (
        "<html><body><article><h1>Title</h1>"
        "<p>Zero\u200bwidth and \u202ebidi\u202c controls inside this paragraph of reasonably long "
        "text so that the extractor keeps it as the main content of the page.</p>"
        "<p>A second paragraph with more words follows here, to make the article look real "
        "enough for the heuristics of the extraction library to accept it.</p>"
        "</article></body></html>"
    )
    respx.get("https://example.com/z").respond(200, text=html, headers=HTML)
    out = fetch_one("https://example.com/z")
    assert out.content is not None
    assert "Zerowidth" in out.content and "bidi controls" in out.content
    assert not any(ch in out.content for ch in "\u200b\u202e\u202c")


@respx.mock
def test_response_over_2mb_is_skipped_by_streaming() -> None:
    big = b"<html><body><article>" + b"x" * (MAX_RESPONSE_BYTES + 10) + b"</article></body></html>"
    respx.get("https://example.com/big").respond(200, content=big, headers=HTML)
    assert fetch_one("https://example.com/big").content is None


@respx.mock
def test_response_over_2mb_is_skipped_by_content_length() -> None:
    headers = {**HTML, "content-length": str(MAX_RESPONSE_BYTES + 1)}
    route = respx.get("https://example.com/big2").mock(
        return_value=httpx.Response(200, content=b"small", headers=headers)
    )
    assert fetch_one("https://example.com/big2").content is None
    assert route.called


@respx.mock
@pytest.mark.parametrize("ctype", ["application/pdf", "application/json", "image/png", ""])
def test_non_html_content_types_are_skipped(ctype: str) -> None:
    headers = {"content-type": ctype} if ctype else {}
    respx.get("https://example.com/f").respond(
        200, content=page("article_en.html"), headers=headers
    )
    assert fetch_one("https://example.com/f").content is None


@respx.mock
def test_error_status_and_timeout_are_skipped() -> None:
    respx.get("https://example.com/404").respond(404, content=b"nope", headers=HTML)
    respx.get("https://example.com/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("https://example.com/down").mock(side_effect=httpx.ConnectError("refused"))
    for path in ("404", "slow", "down"):
        assert fetch_one(f"https://example.com/{path}").content is None


@respx.mock
def test_overall_deadline_stops_slow_body(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get("https://example.com/drip").respond(
        200, content=page("article_en.html"), headers=HTML
    )
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(fetch.time, "monotonic", lambda: next(ticks))
    assert fetch_one("https://example.com/drip", timeout_s=1.0).content is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "http://localhost/admin",
        "http://127.0.0.1:8080/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://user:pw@example.com/",
        "not a url",
        "",
    ],
)
@respx.mock
def test_forbidden_urls_never_hit_the_network(url: str) -> None:
    route = respx.route().respond(200, content=page("article_en.html"), headers=HTML)
    assert fetch_one(url).content is None
    assert not route.called


@respx.mock
def test_host_resolving_to_private_address_is_blocked() -> None:
    route = respx.get("https://rebind.example.com/").respond(
        200, content=page("article_en.html"), headers=HTML
    )
    assert fetch_one("https://rebind.example.com/", resolver=lambda h: ["10.1.2.3"]).content is None
    mixed = fetch_one("https://rebind.example.com/", resolver=lambda h: ["93.184.216.34", "::1"])
    assert mixed.content is None
    assert fetch_one("https://rebind.example.com/", resolver=lambda h: []).content is None
    assert not route.called


@respx.mock
@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/secret",
        "http://localhost:9000/",
        "http://169.254.169.254/latest/",
        "file:///C:/Windows/win.ini",
        "ftp://example.org/x",
    ],
)
def test_redirect_to_forbidden_target_is_blocked(target: str) -> None:
    respx.get("https://example.com/r").respond(302, headers={"location": target})
    internal = respx.route(host__regex=r"127\.0\.0\.1|localhost|169\.254\.169\.254").respond(
        200, content=page("article_en.html"), headers=HTML
    )
    assert fetch_one("https://example.com/r").content is None
    assert not internal.called


@respx.mock
def test_redirect_to_public_page_is_followed_and_relative_location_works() -> None:
    respx.get("https://example.com/old").respond(301, headers={"location": "/new"})
    respx.get("https://example.com/new").respond(200, content=page("article_en.html"), headers=HTML)
    out = fetch_one("https://example.com/old")
    assert out.content is not None and "tides" in out.content


@respx.mock
def test_redirect_hop_resolving_to_private_address_is_blocked() -> None:
    respx.get("https://example.com/r").respond(302, headers={"location": "https://evil.test/x"})
    target = respx.get("https://evil.test/x").respond(
        200, content=page("article_en.html"), headers=HTML
    )

    def resolver(host: str) -> list[str]:
        return ["192.168.0.9"] if host == "evil.test" else ["93.184.216.34"]

    assert fetch_one("https://example.com/r", resolver=resolver).content is None
    assert not target.called


@respx.mock
def test_redirect_loop_is_cut_off() -> None:
    route = respx.get("https://example.com/loop").respond(
        302, headers={"location": "https://example.com/loop"}
    )
    assert fetch_one("https://example.com/loop").content is None
    assert route.call_count == fetch.MAX_REDIRECTS + 1


@respx.mock
def test_failing_pages_do_not_break_the_others_and_order_is_kept() -> None:
    respx.get("https://a.example.com/").respond(200, content=page("article_en.html"), headers=HTML)
    respx.get("https://b.example.com/").mock(side_effect=httpx.ConnectError("boom"))
    respx.get("https://c.example.com/").respond(200, content=page("article_ru.html"), headers=HTML)
    results = [SearchResult(n, f"https://{n}.example.com/", "s") for n in "abcd"]
    out = fetch_and_extract(results, max_pages=3, resolver=public_resolver)
    assert [r.title for r in out] == ["a", "b", "c", "d"]
    assert [r.content is not None for r in out] == [
        True,
        False,
        True,
        False,
    ]  # d is beyond max_pages


@respx.mock
def test_only_max_pages_are_fetched_and_zero_means_none() -> None:
    route = respx.get(host__regex=r".*\.example\.com").respond(
        200, content=page("article_en.html"), headers=HTML
    )
    results = [SearchResult("t", f"https://h{i}.example.com/", "s") for i in range(5)]
    out = fetch_and_extract(results, max_pages=2, resolver=public_resolver)
    assert route.call_count == 2 and len(out) == 5
    route.reset()
    assert fetch_and_extract(results, max_pages=0, resolver=public_resolver) == results
    assert fetch_and_extract([], resolver=public_resolver) == []
    assert not route.called


@respx.mock
def test_pages_are_fetched_in_parallel() -> None:
    delay = 0.4

    def slow(request: httpx.Request) -> httpx.Response:
        time.sleep(delay)
        return httpx.Response(200, content=page("article_en.html"), headers=HTML)

    respx.get(host__regex=r".*\.example\.com").mock(side_effect=slow)
    results = [SearchResult("t", f"https://h{i}.example.com/", "s") for i in range(3)]
    started = time.monotonic()
    out = fetch_and_extract(results, max_pages=3, resolver=public_resolver)
    elapsed = time.monotonic() - started
    assert all(r.content for r in out)
    assert elapsed < delay * 3 * 0.75


def test_extraction_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    import trafilatura

    def boom(*a: object, **k: object) -> str:
        raise RuntimeError("lxml exploded")

    monkeypatch.setattr(trafilatura, "extract", boom)
    assert fetch.extract_text("<html></html>") is None
