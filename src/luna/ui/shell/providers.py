"""Models / providers dialog logic without Qt (ADR 0011, Feature D).

``check_provider`` is the dialog's *Test* (blocking: run it in a worker thread);
``ProvidersPresenter`` owns the ordered provider list, key status/save and the ``providers.toml``
write. Keys are never logged or returned for display; a saved or tested key is registered with
the log redaction filter at once.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from luna.config.providers_file import write_overlay
from luna.config.secrets import CredentialStore
from luna.config.settings import ProviderSettings, Settings
from luna.core.errors import ConfigError
from luna.llm.presets import LOCAL_HOSTS, ProviderPreset, validate_base_url
from luna.logs.redact import register_secret
from luna.ui.shell.keys import TEST_TIMEOUT_S, KeyCheck
from luna.ui.shell.strings import DEFAULT_LANG, t

PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_MODELS = 500
PROBE_MAX_TOKENS = 32

_PING_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Replies with pong.",
        "parameters": {"type": "object", "properties": {}},
    },
}


class ToolSupport(StrEnum):
    YES = "yes"
    UNCLEAR = "unclear"
    NO = "no"


@dataclass(frozen=True)
class ProviderCheck:
    key: KeyCheck
    tools: ToolSupport | None = None  # None: not probed (key failed) or the probe errored
    latency_ms: int | None = None
    models: tuple[str, ...] = ()


def _parse_models(base_url: str, payload: Any) -> tuple[str, ...]:
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ()
    openrouter = (urlsplit(base_url).hostname or "") == "openrouter.ai"
    found: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id = item["id"]
        if openrouter:  # only free models that can call tools are useful here
            params = item.get("supported_parameters")
            if not model_id.endswith(":free") or not isinstance(params, list):
                continue
            if "tools" not in params:
                continue
        found.append(model_id)
    return tuple(sorted(found)[:MAX_MODELS])


_NO_TOOLS_CUES = (
    "not support",
    "unsupported",
    "does not",
    "doesn't",
    "no endpoints",
    "not available",
    "unknown parameter",
    "unrecognized",
    "extra inputs",
    "not allowed",
)


def _probe_tools(response: httpx.Response) -> ToolSupport | None:
    if response.status_code == 200:
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            return ToolSupport.UNCLEAR
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        return ToolSupport.YES if calls else ToolSupport.UNCLEAR
    if response.status_code in (400, 422):
        text = response.text.lower()
        mentions = "tool" in text or "function" in text
        if mentions and any(cue in text for cue in _NO_TOOLS_CUES):
            return ToolSupport.NO
    return None


def check_provider(
    base_url: str,
    model: str,
    key: str | None,
    *,
    timeout: float = TEST_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> ProviderCheck:
    """Key check (``GET /models``) then a tool-calling probe (``POST /chat/completions``).

    Blocking, at most ``timeout`` seconds per request. The key goes only to ``base_url``'s host.
    """
    try:
        base = validate_base_url(base_url).rstrip("/")
    except ValueError:
        return ProviderCheck(KeyCheck.ERROR)
    key = (key or "").strip()
    if key:
        register_secret(key)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with httpx.Client(timeout=timeout, transport=transport, trust_env=False) as client:
            listing = client.get(f"{base}/models", headers=headers)
            if listing.status_code in (401, 403):
                return ProviderCheck(KeyCheck.REJECTED)
            models: tuple[str, ...] = ()
            if listing.status_code == 200:
                try:
                    models = _parse_models(base, listing.json())
                except ValueError:
                    models = ()
            elif listing.status_code != 404:  # some providers have no /models: still probe chat
                return ProviderCheck(KeyCheck.ERROR)
            if not model.strip():
                return ProviderCheck(KeyCheck.OK, models=models)
            body = {
                "model": model.strip(),
                "messages": [{"role": "user", "content": "Call the ping tool."}],
                "tools": [_PING_TOOL],
                "max_tokens": PROBE_MAX_TOKENS,
            }
            started = time.monotonic()
            try:
                answer = client.post(f"{base}/chat/completions", headers=headers, json=body)
            except httpx.HTTPError:
                return ProviderCheck(KeyCheck.OK, models=models)
            latency = round((time.monotonic() - started) * 1000)
    except httpx.HTTPError:
        return ProviderCheck(KeyCheck.NO_CONNECTION)
    if answer.status_code in (401, 403):
        return ProviderCheck(KeyCheck.REJECTED, models=models)
    tools = _probe_tools(answer)
    return ProviderCheck(KeyCheck.OK, tools, latency if answer.status_code == 200 else None, models)


@dataclass
class ProviderDraft:
    """One row of the dialog: an editable copy of ``[llm.providers.<name>]``."""

    name: str
    conf: ProviderSettings
    enabled: bool
    preset: ProviderPreset | None = field(default=None)

    @property
    def custom(self) -> bool:
        return self.preset is None


def _is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOCAL_HOSTS


class ProvidersPresenter:
    """Ordered provider list (chain first, then the disabled ones) with save/test helpers."""

    def __init__(
        self,
        settings: Settings,
        store: CredentialStore,
        presets: Sequence[ProviderPreset],
        overlay: Path,
        *,
        lang: str = DEFAULT_LANG,
        order_overridden: bool = False,
    ) -> None:
        self.lang = lang
        self.order_overridden = order_overridden  # luna.toml also sets [llm] chain
        self._settings = settings
        self._store = store
        self.presets = tuple(presets)
        self._overlay = overlay
        by_url = {p.base_url.rstrip("/"): p for p in self.presets}
        chain = list(dict.fromkeys(settings.llm.chain))
        names = [*chain, *(n for n in settings.llm.providers if n not in chain)]
        self.drafts: list[ProviderDraft] = [
            ProviderDraft(
                name,
                settings.llm.providers[name].model_copy(),
                name in chain,
                by_url.get(settings.llm.providers[name].base_url.rstrip("/")),
            )
            for name in names
        ]

    # --- list editing -------------------------------------------------------------------------

    def move(self, index: int, delta: int) -> int:
        """Move a row up (-1) or down (+1); returns its new index (unchanged at the ends)."""
        target = index + delta
        if not 0 <= target < len(self.drafts):
            return index
        self.drafts[index], self.drafts[target] = self.drafts[target], self.drafts[index]
        return target

    def set_enabled(self, index: int, enabled: bool) -> None:
        self.drafts[index].enabled = enabled

    def remove(self, index: int) -> None:
        """Drop a row. A built-in or luna.toml provider reappears (disabled) at the next open."""
        del self.drafts[index]

    def unique_name(self, wanted: str) -> str:
        names = {d.name for d in self.drafts}
        if wanted not in names:
            return wanted
        n = 2
        while f"{wanted}_{n}"[:32] in names:
            n += 1
        return f"{wanted}_{n}"[:32]

    def add_from_preset(self, preset: ProviderPreset) -> int:
        name = self.unique_name(preset.id)
        conf = ProviderSettings(
            base_url=preset.base_url,
            model=preset.default_model or "",
            api_key_env=preset.key_env,
            **preset.quirks,
        )
        self.drafts.append(ProviderDraft(name, conf, True, preset))
        return len(self.drafts) - 1

    def add_custom(
        self, name: str, base_url: str, model: str, *, needs_key: bool | None = None
    ) -> int:
        """Add a custom OpenAI-compatible provider. ``ValueError`` for a bad name or URL."""
        name = name.strip().lower()
        if not PROVIDER_NAME_PATTERN.match(name):
            raise ValueError(t("providers.name_rule", self.lang))
        if name in {d.name for d in self.drafts}:
            raise ValueError(t("providers.name_taken", self.lang))
        url = validate_base_url(base_url)
        if needs_key is None:
            needs_key = not _is_local(url)
        conf = ProviderSettings(
            base_url=url,
            model=model.strip(),
            api_key_env=f"LUNA_{name.upper()}_API_KEY" if needs_key else None,
        )
        self.drafts.append(ProviderDraft(name, conf, True, None))
        return len(self.drafts) - 1

    def set_model(self, index: int, model: str) -> None:
        draft = self.drafts[index]
        model = model.strip()
        if model != draft.conf.model:  # a different model: the probe result no longer applies
            draft.conf = draft.conf.model_copy(update={"model": model, "supports_tools": None})

    def set_base_url(self, index: int, base_url: str) -> None:
        """Only custom providers have an editable URL. ``ValueError`` for a bad one."""
        draft = self.drafts[index]
        if not draft.custom:
            raise ValueError(t("providers.url_fixed", self.lang))
        draft.conf = draft.conf.model_copy(
            update={"base_url": validate_base_url(base_url), "supports_tools": None}
        )

    # --- display ------------------------------------------------------------------------------

    def badge(self, index: int) -> str:
        preset = self.drafts[index].preset
        return t(f"providers.badge.{preset.free}", self.lang) if preset else ""

    def note(self, index: int) -> str:
        preset = self.drafts[index].preset
        return preset.free_note if preset else ""

    def key_url(self, index: int) -> str | None:
        preset = self.drafts[index].preset
        return preset.key_url if preset else None

    def needs_key(self, index: int) -> bool:
        return bool((self.drafts[index].conf.api_key_env or "").strip())

    def warnings(self, index: int) -> list[str]:
        draft = self.drafts[index]
        found = []
        if draft.preset is not None and draft.preset.free == "paid":
            found.append(t("providers.warn.paid", self.lang))
        if draft.preset is not None and draft.preset.free == "trial":
            found.append(t("providers.warn.trial", self.lang))
        if draft.conf.supports_tools is False:
            found.append(t("providers.warn.no_tools", self.lang))
        elif draft.conf.supports_tools is None:
            found.append(t("providers.warn.untested", self.lang))
        return found

    def key_status(self, index: int) -> str:
        env_name = (self.drafts[index].conf.api_key_env or "").strip()
        if not env_name:
            return t("providers.key.none", self.lang)
        saved = self._store.get(env_name)
        active = self._settings.api_key(env_name)
        if active is not None and active.get_secret_value() != saved:
            if saved:
                return t("providers.key.overridden", self.lang)
            return t("providers.key.env", self.lang)
        return t("providers.key.saved" if saved else "providers.key.not_set", self.lang)

    # --- keys and tests -----------------------------------------------------------------------

    def save_key(self, index: int, value: str) -> bool:
        """Save a non-blank key to the credential store. False for a blank value or no key name."""
        env_name = (self.drafts[index].conf.api_key_env or "").strip()
        value = value.strip()
        if not value or not env_name:
            return False
        self._store.set(env_name, value)
        register_secret(value)
        return True

    def key_for_test(self, index: int, typed: str) -> str | None:
        """The typed key, else a saved or environment one (for the Test button)."""
        typed = typed.strip()
        if typed:
            return typed
        env_name = (self.drafts[index].conf.api_key_env or "").strip()
        if not env_name:
            return None
        active = self._settings.api_key(env_name)
        return self._store.get(env_name) or (active.get_secret_value() if active else None)

    def apply_check(self, name: str, base_url: str, model: str, check: ProviderCheck) -> int | None:
        """Record a finished test for the row ``name``; its index, or None if the result is stale.

        Stale: the row was removed, or its base URL (or, for a test with a model, its model)
        changed while the test ran.
        """
        for index, draft in enumerate(self.drafts):
            if draft.name != name:
                continue
            if draft.conf.base_url != base_url or (model and draft.conf.model != model):
                return None
            self.record_check(index, check)
            return index
        return None

    def record_check(self, index: int, check: ProviderCheck) -> None:
        """Remember the probe result as ``supports_tools`` (unclear / no probe: unknown)."""
        if check.tools is None:
            return
        value = {ToolSupport.YES: True, ToolSupport.NO: False}.get(check.tools)
        draft = self.drafts[index]
        draft.conf = draft.conf.model_copy(update={"supports_tools": value})

    # --- saving -------------------------------------------------------------------------------

    def save(self) -> Path:
        """Write ``providers.toml``. ``ConfigError`` (nothing written) when invalid."""
        chain = [d.name for d in self.drafts if d.enabled]
        if not chain:
            raise ConfigError(t("providers.err.no_provider", self.lang))
        for draft in self.drafts:
            if draft.enabled and not draft.conf.model.strip():
                raise ConfigError(t("providers.err.no_model", self.lang, name=draft.name))
        providers = {d.name: d.conf for d in self.drafts}
        write_overlay(self._overlay, chain, providers, base=self._settings)
        return self._overlay
