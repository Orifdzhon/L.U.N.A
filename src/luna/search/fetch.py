"""Safe page fetching and main-text extraction for search results (ADR 0006, ARCHITECTURE s9).

Fetched pages are untrusted input. Nothing here executes or renders anything: the body is
downloaded as bytes (hard size cap, overall deadline), decoded, run through ``trafilatura``
and sanitized. A page that fails for any reason is skipped, never fatal.

Network safety:
- only ``http``/``https`` URLs that pass ``validate_web_url``;
- localhost / private / link-local / reserved hosts are refused, both as literals and after DNS
  resolution (every resolved address must be public);
- redirects are followed by hand, at most ``MAX_REDIRECTS`` hops, and every hop is validated
  the same way (a public page redirecting to ``http://127.0.0.1`` or ``file://`` is dropped);
- only ``text/html`` / ``application/xhtml+xml`` bodies are read.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import httpx

from luna.platform.browser import is_local_host, validate_web_url
from luna.search.base import SearchResult
from luna.search.sanitize import clean_text

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 2000
MAX_REDIRECTS = 3
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
USER_AGENT = "Mozilla/5.0 (compatible; LUNA-assistant/0.1; +https://github.com/Orifdzhon/L.U.N.A)"

Resolver = Callable[[str], list[str]]


class _Skip(Exception):
    """The page is not fetched; the message is for debug logs only (never shown to the user)."""


def resolve_host(host: str) -> list[str]:
    """All IP addresses ``host`` resolves to (empty on failure)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return sorted({str(info[4][0]) for info in infos})


def _check_url(url: str, resolver: Resolver) -> str:
    """Return the normalized URL, or raise ``_Skip`` if it must not be fetched."""
    try:
        normalized = validate_web_url(url)
    except ValueError as exc:
        raise _Skip(f"invalid url: {exc}") from exc
    host = urlsplit(normalized).hostname or ""
    if is_local_host(host):
        raise _Skip("local host")
    try:
        ipaddress.ip_address(host)
    except ValueError:  # a name: every address it resolves to must be public
        addresses = resolver(host)
        if not addresses:
            raise _Skip("host does not resolve") from None
        for address in addresses:
            if is_local_host(address.split("%", 1)[0]):
                raise _Skip("host resolves to a non-public address") from None
    return normalized


def _media_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _declared_charset(headers: httpx.Headers, body: bytes) -> str | None:
    match = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", headers.get("content-type", ""), re.I)
    if match is None:
        match = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", body[:4096], re.I)
        return match.group(1).decode("ascii", "ignore") if match else None
    return match.group(1)


def _decode(headers: httpx.Headers, body: bytes) -> str:
    candidates: list[str] = []
    if (declared := _declared_charset(headers, body)) is not None:
        candidates.append(declared)
    candidates.append("utf-8")
    for name in candidates:
        try:
            return body.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
    from trafilatura.utils import detect_encoding

    for name in detect_encoding(body[:65536]):
        try:
            return body.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _download(client: httpx.Client, url: str, deadline: float, resolver: Resolver) -> str:
    """Fetch ``url`` (following validated redirects) and return the decoded HTML."""
    current = _check_url(url, resolver)
    for _ in range(MAX_REDIRECTS + 1):
        with client.stream("GET", current, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise _Skip("redirect without location")
                current = _check_url(urljoin(current, location), resolver)
                continue
            if response.status_code != 200:
                raise _Skip(f"http {response.status_code}")
            if _media_type(response.headers) not in HTML_TYPES:
                raise _Skip("not html")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise _Skip("response too large")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _Skip("response too large")
                if time.monotonic() > deadline:
                    raise _Skip("deadline exceeded")
                chunks.append(chunk)
            return _decode(response.headers, b"".join(chunks))
    raise _Skip("too many redirects")


def extract_text(html: str) -> str | None:
    """Main text of an HTML document, sanitized and capped (``None`` if nothing useful)."""
    import trafilatura

    try:
        text = trafilatura.extract(
            html,
            output_format="txt",
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_links=False,
            favor_precision=True,
        )
    except Exception:  # lxml / trafilatura internals on hostile markup
        log.debug("text extraction failed", exc_info=True)
        return None
    if not text:
        return None
    return clean_text(text, max_chars=MAX_CONTENT_CHARS) or None


def _fetch_one(
    client: httpx.Client, result: SearchResult, timeout_s: float, resolver: Resolver
) -> SearchResult:
    try:
        html = _download(client, result.url, time.monotonic() + timeout_s, resolver)
        content = extract_text(html)
    except _Skip as exc:
        log.debug("page skipped (%s)", exc)
        return result
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
        log.debug("page fetch failed: %s", type(exc).__name__)
        return result
    except Exception:  # a broken page must never break the search
        log.debug("unexpected page fetch error", exc_info=True)
        return result
    return replace(result, content=content) if content else result


def fetch_and_extract(
    results: Sequence[SearchResult],
    *,
    max_pages: int = 3,
    timeout_s: float = 4.0,
    resolver: Resolver = resolve_host,
) -> list[SearchResult]:
    """Fetch the first ``max_pages`` results in parallel and fill in ``content``.

    Order and length of ``results`` are preserved. A page that cannot be fetched (bad URL,
    local host, redirect to a forbidden target, timeout, > 2 MB, non-HTML, error status,
    extraction failure) keeps ``content=None``; no exception escapes. Output is untrusted.
    """
    items = list(results)
    count = min(max(max_pages, 0), len(items))
    if count == 0:
        return items
    timeout = httpx.Timeout(timeout_s)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9"}
    with (
        httpx.Client(timeout=timeout, headers=headers, trust_env=False) as client,
        ThreadPoolExecutor(max_workers=count, thread_name_prefix="luna-fetch") as pool,
    ):
        fetched = list(
            pool.map(lambda r: _fetch_one(client, r, timeout_s, resolver), items[:count])
        )
    return fetched + items[count:]
