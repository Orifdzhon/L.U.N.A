"""Default-browser opener (OS-neutral, stdlib ``webbrowser``).

Why ``webbrowser.open`` and nothing else: on Windows it ends up in ``os.startfile(url)``
(ShellExecute with the "open" verb), which hands the URL to the default browser as one
argument. No shell is involved and no command line is ever built from the URL. The URL is
validated and normalized here again (defense in depth, independent of the tool): only
``http``/``https`` with a host, no credentials, no whitespace, control or shell-special
characters (``" < > ^ ` | { } \\``; ``&`` stays legal in queries and is harmless because the
URL is one ShellExecute argument, never passed through ``cmd``), and the host is always opened
in its IDNA/punycode ASCII form (mixed-script and fullwidth hosts are refused).
"""

from __future__ import annotations

import unicodedata
import webbrowser
from urllib.parse import urlsplit, urlunsplit

from luna.core.errors import ToolError

MAX_URL_CHARS = 2048
_ALLOWED_SCHEMES = ("http", "https")


_FORBIDDEN_CHARS = frozenset('\\"<>^`|{}')


def _script(ch: str) -> str:
    return unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0]


def _ascii_host(host: str) -> str:
    """IDNA/punycode ASCII form of ``host``; rejects fullwidth forms and mixed-script labels."""
    if unicodedata.normalize("NFKC", host) != host:
        raise ValueError("host contains compatibility (e.g. fullwidth) characters")
    for label in host.split("."):
        if len({_script(c) for c in label if unicodedata.category(c).startswith("L")}) > 1:
            raise ValueError("host label mixes scripts")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid host: {exc}") from exc


def validate_web_url(url: str) -> str:
    """Return the normalized form of a safe http(s) URL, else raise ``ValueError``.

    Normalization: lowercase scheme, host in IDNA/punycode ASCII form; path, query and
    fragment are kept as given. Callers must open the returned string, not the input.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ValueError("invalid URL")
    if any(ch.isspace() or not ch.isprintable() or ch in _FORBIDDEN_CHARS for ch in url):
        raise ValueError("URL contains whitespace, control or shell-special characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"malformed URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError("only http/https URLs are allowed")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credentials in URL are not allowed")
    if port == 0:
        raise ValueError("invalid port")
    netloc = f"[{host}]" if ":" in host else _ascii_host(host)
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


class WebBrowserOpener:
    """``BrowserOpener`` backed by ``webbrowser.open``."""

    def open_url(self, url: str) -> None:
        url = validate_web_url(url)  # open exactly the normalized string
        try:
            opened = webbrowser.open(url)
        except (webbrowser.Error, OSError) as exc:
            raise ToolError(f"cannot open the browser: {exc}") from exc
        if not opened:
            raise ToolError("cannot open the browser: no browser available")
