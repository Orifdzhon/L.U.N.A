"""DuckDuckGo metasearch via the ``ddgs`` library (ADR 0006).

The library object is injectable (``client_factory``) so unit tests never touch the network.
Everything that comes back is untrusted web text: titles and snippets are sanitized here,
and results are flagged ``untrusted=True`` (see ``SearchResult``).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Protocol

from ddgs.exceptions import DDGSException, RatelimitException

from luna.core.errors import ProviderRequestError, ProviderUnavailable, RateLimited
from luna.platform.browser import validate_web_url
from luna.search.base import SearchResult
from luna.search.sanitize import clean_text

log = logging.getLogger(__name__)

NAME = "ddgs"
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 500
_REGIONS = {"ru": "ru-ru", "en": "us-en"}
_DEFAULT_REGION = "wt-wt"
OVERALL_GRACE_S = 1.0  # search() never takes longer than timeout_s + this


class DdgsClient(Protocol):
    def __enter__(self) -> DdgsClient: ...
    def __exit__(self, *exc_info: object) -> None: ...
    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]: ...


ClientFactory = Callable[[int], DdgsClient]


def _default_client(timeout_s: int) -> DdgsClient:
    from ddgs import DDGS

    return DDGS(timeout=timeout_s)


def is_no_results(exc: Exception) -> bool:
    """``ddgs`` 9.x has no dedicated exception for "nothing found": it raises a plain
    ``DDGSException("No results found.")``. This is the one place that knows the wording
    (pinned by a test against the installed version)."""
    return "no results found" in str(exc).lower()


def region_for(lang: str) -> str:
    """``ru`` -> ``ru-ru``, ``en`` -> ``us-en``, anything else -> worldwide."""
    return _REGIONS.get(lang.strip().lower().replace("_", "-").split("-")[0], _DEFAULT_REGION)


class DdgsSearch:
    name = NAME

    def __init__(self, client_factory: ClientFactory | None = None) -> None:
        self._factory = client_factory or _default_client

    def search(
        self, query: str, *, lang: str, max_results: int = 5, timeout_s: float = 5.0
    ) -> list[SearchResult]:
        if not query or not query.strip():
            raise ProviderRequestError("empty search query")
        if max_results < 1:
            raise ProviderRequestError("max_results must be >= 1")
        ceil_s = max(1, math.ceil(timeout_s))
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="luna-ddgs")
        try:
            future = pool.submit(self._query, query.strip(), lang, max_results, ceil_s)
            raw = future.result(timeout=timeout_s + OVERALL_GRACE_S)
        except FutureTimeout as exc:
            raise ProviderUnavailable("ddgs search timed out") from exc
        except RatelimitException as exc:
            raise RateLimited("ddgs rate limited") from exc
        except DDGSException as exc:
            if is_no_results(exc):
                return []
            raise ProviderUnavailable(f"ddgs search failed: {exc}") from exc
        except Exception as exc:  # network errors, parser breakage inside the library
            raise ProviderUnavailable(f"ddgs search failed: {exc}") from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return self._map(raw, max_results)

    def _query(self, query: str, lang: str, max_results: int, timeout_s: int) -> Any:
        with self._factory(timeout_s) as client:  # always closed, even on errors
            return client.text(
                query, region=region_for(lang), max_results=max_results, backend="auto"
            )

    @staticmethod
    def _map(raw: Any, max_results: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            try:
                url = validate_web_url(str(item.get("href") or ""))
            except ValueError:
                log.debug("ddgs: skipped a result with an unusable URL")
                continue
            results.append(
                SearchResult(
                    title=clean_text(
                        str(item.get("title") or ""),
                        max_chars=MAX_TITLE_CHARS,
                        keep_newlines=False,
                    ),
                    url=url,
                    snippet=clean_text(
                        str(item.get("body") or ""),
                        max_chars=MAX_SNIPPET_CHARS,
                        keep_newlines=False,
                    ),
                )
            )
            if len(results) >= max_results:
                break
        return results
