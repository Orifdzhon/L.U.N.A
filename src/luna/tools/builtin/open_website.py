"""``open_website``: open a site in the default browser (F3, ARCHITECTURE section 9).

The target may be an alias ("ютуб"), a bare domain ("habr.com") or a full http(s) URL.
Whatever is finally handed to the ``BrowserOpener`` is an http/https URL; every other scheme,
local/UNC path, credentials-in-URL or text with whitespace is refused (``reason="unsafe_target"``).
Text that is none of the above yields ``reason="no_match"`` so the router can fall through.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import tomllib
from collections.abc import Mapping
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from luna.core.errors import ConfigError, ToolError
from luna.platform.base import BrowserOpener
from luna.platform.browser import validate_web_url
from luna.tools.base import SafetyLevel, ToolContext, ToolResult

NAME = "open_website"
DEFAULT_HOME_URL = "https://www.google.com"
MAX_TARGET_CHARS = 2048

# Bare names ("habr.com") are accepted only with these TLDs, so file names like calc.exe,
# readme.txt or foo.bat never become URLs. Full http(s):// URLs are not limited by this list.
ALLOWED_BARE_TLDS = frozenset(
    {
        "com", "org", "net", "edu", "gov", "info", "biz", "io", "dev", "app", "ai", "me",
        "tv", "gg", "co", "xyz", "online", "site", "tech", "cloud", "blog", "wiki", "page",
        "ru", "xn--p1ai", "su", "ua", "by", "kz", "uz", "tj", "kg", "am", "az", "ge", "md",
        "de", "uk", "fr", "eu", "us", "ca", "jp", "cn", "in", "it", "es", "nl", "pl", "se",
        "ch", "br", "au", "kr", "tr",
    }
)  # fmt: skip

_SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
_HOST_PORT_RE = re.compile(r"[^/\\?#:]+:\d{1,5}(?:[/?#]|$)")
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD_RE = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")


def normalize_alias(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())  # noqa: RUF001


def default_site_aliases() -> dict[str, str]:
    """Shipped aliases from ``luna/data/site_aliases.toml`` (normalized keys)."""
    raw = (resources.files("luna") / "data" / "site_aliases.toml").read_text(encoding="utf-8")
    return _clean_aliases(tomllib.loads(raw))


def _clean_aliases(table: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, url in table.items():
        try:
            if not isinstance(key, str) or not normalize_alias(key):
                raise ValueError("empty alias")
            if not isinstance(url, str):
                raise ValueError("URL must be a string")
            out[normalize_alias(key)] = validate_web_url(url)
        except ValueError as exc:
            raise ConfigError(f"invalid site alias {key!r}: {exc}") from exc
    return out


def _valid_host(host: str) -> bool:
    try:
        ascii_host = host.rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_host.split(".")
    return (
        len(ascii_host) <= 253
        and len(labels) >= 2
        and all(_LABEL_RE.fullmatch(x) for x in labels)
        and bool(_TLD_RE.fullmatch(labels[-1]))
    )


def _ascii_tld(host: str) -> str:
    try:
        return host.rstrip(".").lower().encode("idna").decode("ascii").rsplit(".", 1)[-1]
    except UnicodeError:
        return ""


def _is_local_host(host: str) -> bool:
    """True for localhost and any loopback/private/link-local/reserved/unspecified address,
    including legacy numeric forms such as ``0x7f.1`` or ``2130706433``."""
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        try:
            addr = ipaddress.IPv4Address(socket.inet_aton(name))
        except (OSError, ValueError):
            return False
    return not addr.is_global


class _Unsafe(Exception):
    pass


def resolve_target(target: str, aliases: Mapping[str, str]) -> str | None:
    """Return the http(s) URL for ``target``, ``None`` if it is just unknown.

    Raises ``_Unsafe`` for anything that looks like a non-web target.
    """
    text = target.strip()
    if not text:
        return None
    if len(text) > MAX_TARGET_CHARS or any(not ch.isprintable() for ch in text):
        raise _Unsafe
    if (url := aliases.get(normalize_alias(text))) is not None:
        return url
    if "\\" in text or text.startswith("//"):  # Windows/UNC paths, protocol-relative URLs
        raise _Unsafe
    if re.match(r"https?://", text, re.IGNORECASE):
        try:
            url = validate_web_url(text)
        except ValueError as exc:
            raise _Unsafe from exc
        if _is_local_host(urlsplit(url).hostname or ""):
            raise _Unsafe
        return url
    if _SCHEME_RE.match(text) and not _HOST_PORT_RE.match(text):
        raise _Unsafe  # javascript:, file:, data:, ftp:, mailto:, C:... and similar
    if any(ch.isspace() for ch in text):
        return None  # free text, not a site
    candidate = text if "://" in text else "https://" + text
    try:
        parts = urlsplit(candidate)
        _ = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None:
        raise _Unsafe
    host = parts.hostname
    if not host:
        return None
    if _is_local_host(host):
        raise _Unsafe  # loopback / private / reserved: only explicit aliases or URLs may
    if not _valid_host(host):
        return None
    if _ascii_tld(host) not in ALLOWED_BARE_TLDS:
        return None  # calc.exe, readme.txt, foo.bat ...: not a website name
    try:
        return validate_web_url(candidate)
    except ValueError as exc:
        raise _Unsafe from exc


class OpenWebsiteTool:
    name = NAME
    description = (
        "Open a website in the default browser. target: a site name (e.g. 'youtube', 'ютуб'), "
        "a domain (e.g. 'habr.com') or an http(s) URL. Omit target to just open the browser."
    )
    parameters: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Site alias, domain or http(s) URL. Empty = open the browser.",
                "maxLength": MAX_TARGET_CHARS,
            }
        },
        "additionalProperties": False,
    }
    safety = SafetyLevel.SAFE

    def __init__(
        self,
        opener: BrowserOpener,
        site_aliases: Mapping[str, str] | None = None,
        *,
        home_url: str = DEFAULT_HOME_URL,
    ) -> None:
        """``site_aliases`` are user overrides (e.g. ``settings.router.site_aliases``)."""
        self._opener = opener
        self._aliases = default_site_aliases()
        self._aliases.update(_clean_aliases(site_aliases or {}))
        try:
            self._home_url = validate_web_url(home_url)
        except ValueError as exc:
            raise ConfigError(f"invalid home_url: {exc}") from exc

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = args.get("target", "")
        if not isinstance(target, str):
            return self._fail("target must be a string", "invalid_target")
        if not target.strip():
            url: str | None = self._home_url
        else:
            try:
                url = resolve_target(target, self._aliases)
            except _Unsafe:
                return self._fail(
                    "Refused: only http/https websites can be opened", "unsafe_target"
                )
            if url is None:
                return self._fail("Unknown website", "no_match")
        host = urlsplit(url).hostname or ""
        try:
            self._opener.open_url(url)
        except ValueError:
            return self._fail("Refused: only http/https websites can be opened", "unsafe_target")
        except ToolError as exc:
            return self._fail(f"Could not open the browser: {exc}", "open_failed")
        return ToolResult(NAME, True, f"Opened {host}", {"host": host})

    @staticmethod
    def _fail(summary: str, reason: str) -> ToolResult:
        return ToolResult(NAME, False, summary, {"reason": reason})
