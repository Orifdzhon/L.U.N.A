# ruff: noqa: RUF001
import inspect
import threading
import time
from typing import Any

import pytest
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from luna.core.errors import ProviderRequestError, ProviderUnavailable, RateLimited
from luna.search.base import SearchProvider
from luna.search.ddgs_search import DdgsSearch, is_no_results, region_for


class FakeDDGS:
    def __init__(self, outcome: list[dict[str, Any]] | Exception) -> None:
        self.outcome = outcome
        self.timeouts: list[int] = []
        self.closed = 0
        self.release = threading.Event()
        self.block = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def factory(self, timeout_s: int) -> "FakeDDGS":
        self.timeouts.append(timeout_s)
        return self

    def __enter__(self) -> "FakeDDGS":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed += 1

    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append((query, kwargs))
        if self.block:
            self.release.wait(10)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make(outcome: list[dict[str, Any]] | Exception) -> tuple[DdgsSearch, FakeDDGS]:
    fake = FakeDDGS(outcome)
    return DdgsSearch(fake.factory), fake


def test_satisfies_protocol() -> None:
    assert isinstance(DdgsSearch(), SearchProvider)
    assert DdgsSearch().name == "ddgs"


def test_maps_results_and_passes_region() -> None:
    provider, fake = make(
        [
            {"title": "Курс доллара", "href": "https://cbr.ru/rates", "body": "Официальный курс"},
            {"title": "T2", "href": "http://example.com/a?b=1", "body": "S2"},
        ]
    )
    results = provider.search("курс доллара", lang="ru", max_results=5, timeout_s=4.2)
    assert [(r.title, r.url, r.snippet) for r in results] == [
        ("Курс доллара", "https://cbr.ru/rates", "Официальный курс"),
        ("T2", "http://example.com/a?b=1", "S2"),
    ]
    assert all(r.content is None and r.untrusted for r in results)
    query, kwargs = fake.calls[0]
    assert query == "курс доллара"
    assert kwargs["region"] == "ru-ru" and kwargs["max_results"] == 5
    assert fake.timeouts == [5]


def test_max_results_is_respected_even_if_library_returns_more() -> None:
    raw = [{"title": str(i), "href": f"https://example.com/{i}", "body": ""} for i in range(10)]
    provider, _ = make(raw)
    assert len(provider.search("q", lang="en", max_results=3)) == 3


def test_skips_results_with_unusable_urls() -> None:
    provider, _ = make(
        [
            {"title": "bad", "href": "javascript:alert(1)", "body": ""},
            {"title": "file", "href": "file:///C:/x", "body": ""},
            {"title": "none", "body": ""},
            {"title": "ok", "href": "https://example.com/", "body": ""},
        ]
    )
    assert [r.title for r in provider.search("q", lang="en")] == ["ok"]


def test_invisible_characters_removed_from_title_and_snippet() -> None:
    provider, _ = make(
        [{"title": "a\u200bb\u202ec", "href": "https://example.com/", "body": "x\u2060 y\x00z"}]
    )
    (result,) = provider.search("q", lang="en")
    assert result.title == "abc"
    assert result.snippet == "x yz"


def test_rate_limit_maps_to_rate_limited() -> None:
    provider, _ = make(RatelimitException("202 Ratelimit"))
    with pytest.raises(RateLimited):
        provider.search("q", lang="en")


@pytest.mark.parametrize(
    "error",
    [
        TimeoutException("timed out"),
        DDGSException("engine broke"),
        ConnectionError("network down"),
        RuntimeError("parser changed"),
    ],
)
def test_library_failures_map_to_provider_unavailable(error: Exception) -> None:
    provider, _ = make(error)
    with pytest.raises(ProviderUnavailable):
        provider.search("q", lang="en")


def test_no_results_is_empty_list_not_error() -> None:
    provider, _ = make(DDGSException("No results found."))
    assert provider.search("q", lang="en") == []


def test_client_is_closed_after_success_and_after_errors() -> None:
    ok, fake_ok = make([])
    ok.search("q", lang="en")
    assert fake_ok.closed == 1
    bad, fake_bad = make(RatelimitException("x"))
    with pytest.raises(RateLimited):
        bad.search("q", lang="en")
    assert fake_bad.closed == 1


def test_overall_timeout_bounds_a_hanging_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("luna.search.ddgs_search.OVERALL_GRACE_S", 0.1)
    provider, fake = make([])
    fake.block = True
    started = time.monotonic()
    try:
        with pytest.raises(ProviderUnavailable, match="timed out"):
            provider.search("q", lang="en", timeout_s=0.2)
        assert time.monotonic() - started < 2
    finally:
        fake.release.set()


def test_no_results_wording_matches_installed_library() -> None:
    import ddgs.ddgs

    assert is_no_results(DDGSException("No results found."))
    assert not is_no_results(DDGSException("engine broke"))
    # If a ddgs bump changes the wording this fails and the mapping must be revisited.
    assert "No results found." in inspect.getsource(ddgs.ddgs)


def test_client_factory_failure_is_unavailable() -> None:
    def broken(_: int) -> FakeDDGS:
        raise OSError("cannot start")

    with pytest.raises(ProviderUnavailable):
        DdgsSearch(broken).search("q", lang="en")


@pytest.mark.parametrize(("query", "max_results"), [("", 5), ("   ", 5), ("q", 0)])
def test_bad_arguments_are_request_errors(query: str, max_results: int) -> None:
    provider, fake = make([])
    with pytest.raises(ProviderRequestError):
        provider.search(query, lang="en", max_results=max_results)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("lang", "region"),
    [("ru", "ru-ru"), ("ru-RU", "ru-ru"), ("en", "us-en"), ("EN_us", "us-en"), ("de", "wt-wt")],
)
def test_region_for(lang: str, region: str) -> None:
    assert region_for(lang) == region


@pytest.mark.live
def test_live_search_returns_results() -> None:
    results = DdgsSearch().search("курс доллара", lang="ru", max_results=3, timeout_s=8.0)
    assert results
    assert all(r.url.startswith("http") for r in results)
