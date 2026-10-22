"""Default-browser opener (OS-neutral, stdlib ``webbrowser``).

Why ``webbrowser.open`` and nothing else: on Windows it ends up in ``os.startfile(url)``
(ShellExecute with the "open" verb), which hands the URL to the default browser as one
argument. No shell is involved and no command line is ever built from the URL. The URL is
validated here again (defense in depth, independent of the tool): only ``http``/``https`` with
a host, no credentials, no whitespace or control characters, no backslashes.
"""

from __future__ import annotations

import webbrowser
from urllib.parse import urlsplit

from luna.core.errors import ToolError

MAX_URL_CHARS = 2048
_ALLOWED_SCHEMES = ("http", "https")


def validate_web_url(url: str) -> str:
    """Return ``url`` unchanged if it is a safe http(s) URL, else raise ``ValueError``."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ValueError("invalid URL")
    if any(ch.isspace() or not ch.isprintable() or ch == "\\" for ch in url):
        raise ValueError("URL contains whitespace, control characters or backslashes")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"malformed URL: {exc}") from exc
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("only http/https URLs are allowed")
    if not parts.hostname:
        raise ValueError("URL has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credentials in URL are not allowed")
    if port == 0:
        raise ValueError("invalid port")
    return url


class WebBrowserOpener:
    """``BrowserOpener`` backed by ``webbrowser.open``."""

    def open_url(self, url: str) -> None:
        validate_web_url(url)
        try:
            opened = webbrowser.open(url)
        except (webbrowser.Error, OSError) as exc:
            raise ToolError(f"cannot open the browser: {exc}") from exc
        if not opened:
            raise ToolError("cannot open the browser: no browser available")
