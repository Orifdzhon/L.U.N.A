"""API keys dialog logic without Qt: key check, save, status text (ADR 0010 D3).

Keys are never logged or returned for display. A saved or tested key is registered with the log
redaction filter at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx

from luna.config.secrets import CredentialStore
from luna.config.settings import Settings
from luna.logs.redact import register_secret

TEST_TIMEOUT_S = 10.0


class KeyCheck(StrEnum):
    OK = "OK"
    REJECTED = "Rejected (401/403)"
    NO_CONNECTION = "No connection"
    ERROR = "Unexpected answer"


@dataclass(frozen=True)
class KeyField:
    env_name: str  # also the credential name
    label: str
    required: bool
    models_url: str


def has_cloud_llm_key(settings: Settings) -> bool:
    """True when at least one LLM provider that needs a key has one."""
    return any(
        settings.api_key(p.api_key_env) is not None
        for p in settings.llm.providers.values()
        if (p.api_key_env or "").strip()
    )


def key_fields(settings: Settings) -> list[KeyField]:
    """Groq (required for the cloud defaults) and Gemini (optional), URLs from the config."""
    specs = (("groq", "Groq key", True), ("gemini", "Gemini key (optional)", False))
    fields = []
    for name, label, required in specs:
        provider = settings.llm.providers.get(name)
        if provider is None or not (provider.api_key_env or "").strip():
            continue
        url = provider.base_url.rstrip("/") + "/models"
        fields.append(KeyField(provider.api_key_env.strip(), label, required, url))
    return fields


def check_key(
    url: str,
    key: str,
    *,
    timeout: float = TEST_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> KeyCheck:
    """GET the provider's models endpoint with the key. Blocking: run it in a worker thread."""
    key = key.strip()
    register_secret(key)
    try:
        with httpx.Client(timeout=timeout, transport=transport, trust_env=False) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        return KeyCheck.NO_CONNECTION
    if response.status_code == 200:
        return KeyCheck.OK
    if response.status_code in (401, 403):
        return KeyCheck.REJECTED
    return KeyCheck.ERROR


class KeysPresenter:
    def __init__(self, settings: Settings, store: CredentialStore) -> None:
        self._settings = settings
        self._store = store
        self.fields = key_fields(settings)

    def status(self, field: KeyField) -> str:
        saved = self._store.get(field.env_name)
        active = self._settings.api_key(field.env_name)
        if active is not None and active.get_secret_value() != saved:
            if saved:
                return "saved; the key from environment / .env overrides it"
            return "set in environment / .env"
        if saved:
            return "saved"
        return "not set"

    def save(self, field: KeyField, value: str) -> bool:
        """Save a non-blank key. Returns False for a blank value (nothing changes)."""
        value = value.strip()
        if not value:
            return False
        self._store.set(field.env_name, value)
        register_secret(value)
        return True
