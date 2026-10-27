"""Saved API keys: the :class:`CredentialStore` protocol and its Windows Credential Manager backend.

One generic credential per env-variable name (``GROQ_API_KEY`` ...) under the service name
``L.U.N.A.``. ``keyring`` is imported lazily, so a missing or broken backend never breaks start.
Values are never logged.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable
from typing import Protocol

logger = logging.getLogger(__name__)

SERVICE_NAME = "L.U.N.A."
KNOWN_KEY_NAMES = ("GROQ_API_KEY", "GEMINI_API_KEY")


class SecretStoreError(Exception):
    """The credential store could not save or delete a value (message holds no secrets)."""


class CredentialStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class KeyringStore:
    """Windows Credential Manager via ``keyring``. ``get`` never raises (no backend = no key)."""

    def __init__(self, service: str = SERVICE_NAME) -> None:
        self._service = service

    def get(self, name: str) -> str | None:
        try:
            import keyring

            value = keyring.get_password(self._service, name)
        except Exception as exc:
            logger.warning("[keys] cannot read %s from the credential store (%s)", name, _kind(exc))
            return None
        return value or None

    def set(self, name: str, value: str) -> None:
        try:
            import keyring

            keyring.set_password(self._service, name, value)
        except Exception as exc:
            raise SecretStoreError(f"cannot save {name} ({_kind(exc)})") from None

    def delete(self, name: str) -> None:
        try:
            import keyring
            from keyring.errors import PasswordDeleteError

            with contextlib.suppress(PasswordDeleteError):  # nothing stored under this name
                keyring.delete_password(self._service, name)
        except Exception as exc:
            raise SecretStoreError(f"cannot delete {name} ({_kind(exc)})") from None


class InMemoryStore:
    """Fake store for tests."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(initial or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name) or None

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def default_store() -> CredentialStore:
    return KeyringStore()


def forget_keys(store: CredentialStore, names: Iterable[str] = KNOWN_KEY_NAMES) -> list[str]:
    """Delete every saved key in ``names``; return the names that were actually saved."""
    removed = []
    for name in names:
        if store.get(name):
            removed.append(name)
        store.delete(name)
    return removed


def _kind(exc: BaseException) -> str:
    return type(exc).__name__
