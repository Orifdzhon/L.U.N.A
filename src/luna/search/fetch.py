"""Safe page fetching and main-text extraction for search results (ADR 0006, ARCHITECTURE s9).

Fetched pages are untrusted input. Nothing here executes or renders anything: the body is
downloaded as bytes (hard size cap on the *decoded* size, compression-ratio cap, one global
deadline), decoded, run through ``trafilatura`` and sanitized. A page that fails for any reason
is skipped, never fatal.

Network safety:
- only ``http``/``https`` URLs that pass ``validate_web_url``;
- localhost / private / link-local / multicast / reserved hosts are refused, both as literals
  and after DNS resolution (every resolved address must be public);
- redirects are followed by hand, at most ``MAX_REDIRECTS`` hops, and every hop is validated
  the same way (a public page redirecting to ``http://127.0.0.1`` or ``file://`` is dropped).
  An https -> http downgrade is allowed on purpose: the content is read-only public text;
- only ``text/html`` / ``application/xhtml+xml`` bodies are read;
- proxies from the environment are ignored (``trust_env=False``): a proxy would resolve names
  itself and make the address checks meaningless.

Time: ``timeout_s`` is a budget for the whole call, not per request. DNS resolution runs in a
helper pool with a timeout, the deadline is checked before every hop, every body chunk and
before extraction, and the caller stops waiting at the deadline even if a worker is stuck
(workers cannot be cancelled; they are abandoned and bounded by their own socket timeouts).

Known residual risk: DNS rebinding between our address check and httpx's own connect
(see ADR 0006, Amendment 1).
"""

from __future__ import annotations

import codecs
import ipaddress
import logging
import re
import socket
import time
import zlib
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura  # imported up front: a cold import inside a worker would eat the budget
from trafilatura.settings import use_config
from trafilatura.utils import detect_encoding

from luna.platform.browser import is_local_host, validate_web_url
from luna.search.base import SearchResult
from luna.search.sanitize import clean_text

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 2000
MAX_REDIRECTS = 3
MAX_COMPRESSION_RATIO = 100
RATIO_SLACK_BYTES = 64 * 1024
MAX_TREE_SIZE = 20_000
RAW_CHUNK_BYTES = 16 * 1024
WAIT_GRACE_S = 0.25
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
USER_AGENT = "Mozilla/5.0 (compatible; LUNA-assistant/0.1; +https://github.com/Orifdzhon/L.U.N.A)"
# Charsets taken from untrusted headers/meta; anything else falls back to UTF-8.
_CHARSET_PREFIXES = (
    "utf-8", "ascii", "cp125", "cp866", "cp437", "koi8", "iso8859", "latin", "gb", "big5",
    "shift_jis", "euc_jp", "euc_kr", "cp932", "cp949", "cp950", "mac_",
)  # fmt: skip

_EXTRACT_CONFIG = use_config()
_EXTRACT_CONFIG.set("DEFAULT", "MAX_TREE_SIZE", str(MAX_TREE_SIZE))
_EXTRACT_CONFIG.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")  # signal-based: unusable in threads

Resolver = Callable[[str], list[str]]
Clock = Callable[[], float]


class _Skip(Exception):
    """The page is not fetched; the message is for debug logs only (never shown to the user)."""


class _Budget:
    """One deadline shared by every step of every page."""

    def __init__(self, seconds: float, clock: Clock) -> None:
        self._clock = clock
        self._deadline = clock() + seconds

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def check(self) -> float:
        left = self.remaining()
        if left <= 0:
            raise _Skip("deadline exceeded")
        return left


def resolve_host(host: str) -> list[str]:
    """All IP addresses ``host`` resolves to (empty on failure)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return sorted({str(info[4][0]) for info in infos})


def _check_url(url: str, resolver: Resolver, dns: ThreadPoolExecutor, budget: _Budget) -> str:
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
        try:
            addresses = dns.submit(resolver, host).result(timeout=budget.check())
        except FutureTimeout:
            raise _Skip("dns timeout") from None
        if not addresses:
            raise _Skip("host does not resolve") from None
        for address in addresses:
            if is_local_host(address.split("%", 1)[0]):
                raise _Skip("host resolves to a non-public address") from None
    return normalized


def _media_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _safe_charset(name: str | None) -> str | None:
    if not name:
        return None
    try:
        canonical = codecs.lookup(name.strip()).name
    except LookupError:
        return None
    return canonical if canonical.startswith(_CHARSET_PREFIXES) else None


def _declared_charset(headers: httpx.Headers, body: bytes) -> str | None:
    match = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", headers.get("content-type", ""), re.I)
    if match is not None:
        return match.group(1)
    match = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", body[:4096], re.I)
    return match.group(1).decode("ascii", "ignore") if match else None


def _decode(headers: httpx.Headers, body: bytes) -> str:
    candidates = [_safe_charset(_declared_charset(headers, body)), "utf-8"]
    for name in candidates:
        if name is None:
            continue
        try:
            return body.decode(name)
        except UnicodeDecodeError:
            continue
    for guess in detect_encoding(body[:65536]):
        if (name := _safe_charset(guess)) is None:
            continue
        try:
            return body.decode(name)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _read_body(response: httpx.Response, budget: _Budget) -> bytes:
    """Read the body, never holding more than ``MAX_RESPONSE_BYTES`` of decoded data.

    The raw stream is read in small chunks and inflated with ``max_length``, so a
    decompression bomb is stopped after at most one cap's worth of output; the output/input
    ratio is capped as well.
    """
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    inflater: zlib._Decompress | None
    if encoding in ("", "identity"):
        inflater = None
    elif encoding in ("gzip", "x-gzip"):
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        inflater = zlib.decompressobj()
    else:
        raise _Skip(f"unsupported content-encoding {encoding}")
    chunks: list[bytes] = []
    total = raw_total = 0
    for raw in response.iter_raw(chunk_size=RAW_CHUNK_BYTES):
        budget.check()
        raw_total += len(raw)
        if inflater is None:
            total += len(raw)
            if total > MAX_RESPONSE_BYTES:
                raise _Skip("response too large")
            chunks.append(raw)
            continue
        pending = raw
        while pending and not inflater.eof:
            out = inflater.decompress(pending, MAX_RESPONSE_BYTES - total + 1)
            total += len(out)
            if total > MAX_RESPONSE_BYTES:
                raise _Skip("response too large")
            if total > raw_total * MAX_COMPRESSION_RATIO + RATIO_SLACK_BYTES:
                raise _Skip("suspicious compression ratio")
            chunks.append(out)
            tail = inflater.unconsumed_tail
            if not out and tail == pending:
                break
            pending = tail
    return b"".join(chunks)


def _download(
    client: httpx.Client, url: str, budget: _Budget, resolver: Resolver, dns: ThreadPoolExecutor
) -> str:
    """Fetch ``url`` (following validated redirects) and return the decoded HTML."""
    current = _check_url(url, resolver, dns, budget)
    for _ in range(MAX_REDIRECTS + 1):
        left = budget.check()
        with client.stream("GET", current, follow_redirects=False, timeout=left) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise _Skip("redirect without location")
                current = _check_url(urljoin(current, location), resolver, dns, budget)
                continue
            if response.status_code != 200:
                raise _Skip(f"http {response.status_code}")
            if _media_type(response.headers) not in HTML_TYPES:
                raise _Skip("not html")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise _Skip("response too large")
            body = _read_body(response, budget)
            return _decode(response.headers, body)
    raise _Skip("too many redirects")


def extract_text(html: str) -> str | None:
    """Main text of an HTML document, sanitized and capped (``None`` if nothing useful)."""
    try:
        text = trafilatura.extract(
            html,
            output_format="txt",
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_links=False,
            favor_precision=True,
            config=_EXTRACT_CONFIG,
        )
    except Exception:  # lxml / trafilatura internals on hostile markup
        log.debug("text extraction failed", exc_info=True)
        return None
    if not text:
        return None
    return clean_text(text, max_chars=MAX_CONTENT_CHARS) or None


def _fetch_one(
    client: httpx.Client,
    result: SearchResult,
    budget: _Budget,
    resolver: Resolver,
    dns: ThreadPoolExecutor,
) -> SearchResult:
    try:
        html = _download(client, result.url, budget, resolver, dns)
        budget.check()  # extraction cannot be interrupted: do not start it out of time
        content = extract_text(html)
    except _Skip as exc:
        log.debug("page skipped (%s)", exc)
        return result
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, zlib.error) as exc:
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
    clock: Clock = time.monotonic,
) -> list[SearchResult]:
    """Fetch the first ``max_pages`` results in parallel and fill in ``content``.

    ``timeout_s`` bounds the whole call. Order and length of ``results`` are preserved. A page
    that cannot be fetched in time (bad URL, local host, redirect to a forbidden target,
    timeout, > 2 MB, non-HTML, error status, extraction failure) keeps ``content=None``; no
    exception escapes. Output is untrusted web text.
    """
    items = list(results)
    count = min(max(max_pages, 0), len(items))
    if count == 0:
        return items
    budget = _Budget(timeout_s, clock)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    client = httpx.Client(timeout=httpx.Timeout(timeout_s), headers=headers, trust_env=False)
    pool = ThreadPoolExecutor(max_workers=count, thread_name_prefix="luna-fetch")
    dns = ThreadPoolExecutor(max_workers=count, thread_name_prefix="luna-dns")
    try:
        futures: list[Future[SearchResult]] = [
            pool.submit(_fetch_one, client, item, budget, resolver, dns) for item in items[:count]
        ]
        wait(futures, timeout=max(budget.remaining(), 0.0) + WAIT_GRACE_S)
        fetched = [
            f.result() if f.done() and f.exception() is None else item
            for f, item in zip(futures, items, strict=False)
        ]
    finally:
        # Do not wait for stuck workers (a blocked DNS lookup cannot be cancelled).
        pool.shutdown(wait=False, cancel_futures=True)
        dns.shutdown(wait=False, cancel_futures=True)
        client.close()
    return fetched + items[count:]
