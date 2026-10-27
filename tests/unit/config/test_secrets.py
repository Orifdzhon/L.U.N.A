"""Credential store, loader order with the store, --forget-keys, redaction of saved keys."""

import logging
import sys
import types

import pytest

from luna.__main__ import main
from luna.config.loader import load_settings
from luna.config.secrets import (
    InMemoryStore,
    KeyringStore,
    SecretStoreError,
    forget_keys,
)
from luna.logs.redact import RedactionFilter, clear_secrets, register_secrets


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_secrets()
    yield
    clear_secrets()


def _key(settings, name):
    secret = settings.api_key(name)
    return secret.get_secret_value() if secret else None


def test_store_is_the_last_source(tmp_path):
    store = InMemoryStore({"GROQ_API_KEY": "from-store-1234", "GEMINI_API_KEY": "gem-store-1234"})
    (tmp_path / ".env").write_text("GROQ_API_KEY=from-dotenv-1234\n", encoding="utf-8")
    s = load_settings(env={}, cwd=tmp_path, store=store)
    assert _key(s, "GROQ_API_KEY") == "from-dotenv-1234"
    assert _key(s, "GEMINI_API_KEY") == "gem-store-1234"


def test_env_beats_dotenv_and_store(tmp_path):
    store = InMemoryStore({"GROQ_API_KEY": "from-store-1234"})
    s = load_settings(env={"GROQ_API_KEY": "from-env-1234"}, cwd=tmp_path, store=store)
    assert _key(s, "GROQ_API_KEY") == "from-env-1234"


def test_appdata_env_beats_store(tmp_path):
    appdata = tmp_path / "ad"
    (appdata / "LUNA").mkdir(parents=True)
    (appdata / "LUNA" / ".env").write_text("GROQ_API_KEY=appdata-1234\n", encoding="utf-8")
    store = InMemoryStore({"GROQ_API_KEY": "from-store-1234"})
    s = load_settings(env={"APPDATA": str(appdata)}, cwd=tmp_path, store=store)
    assert _key(s, "GROQ_API_KEY") == "appdata-1234"


def test_blank_values_are_ignored(tmp_path):
    store = InMemoryStore({"GROQ_API_KEY": "from-store-1234", "GEMINI_API_KEY": "   "})
    s = load_settings(env={"GROQ_API_KEY": "  "}, cwd=tmp_path, store=store)
    assert _key(s, "GROQ_API_KEY") == "from-store-1234"
    assert _key(s, "GEMINI_API_KEY") is None


def test_only_referenced_names_are_looked_up(tmp_path):
    store = InMemoryStore({"OTHER_KEY": "whatever-1234"})
    s = load_settings(env={}, cwd=tmp_path, store=store)
    assert _key(s, "OTHER_KEY") is None


def test_explicit_env_without_store_does_not_touch_default_store(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("default store used")

    monkeypatch.setattr("luna.config.loader.default_store", boom)
    assert _key(load_settings(env={}, cwd=tmp_path), "GROQ_API_KEY") is None


def test_saved_key_is_redacted_in_logs(tmp_path):
    store = InMemoryStore({"GROQ_API_KEY": "plain-secret-value"})
    s = load_settings(env={}, cwd=tmp_path, store=store)
    register_secrets(s.secret_values())
    record = logging.LogRecord("x", logging.INFO, "f", 1, "key=plain-secret-value", None, None)
    RedactionFilter().filter(record)
    assert "plain-secret-value" not in record.getMessage()


def test_forget_keys_deletes_both_names():
    store = InMemoryStore({"GROQ_API_KEY": "a-1234", "GEMINI_API_KEY": "b-1234", "X": "keep-1234"})
    assert forget_keys(store) == ["GROQ_API_KEY", "GEMINI_API_KEY"]
    assert store.values == {"X": "keep-1234"}
    assert forget_keys(store) == []


def test_forget_keys_cli(monkeypatch, capsys):
    store = InMemoryStore({"GROQ_API_KEY": "a-1234", "GEMINI_API_KEY": "b-1234"})
    monkeypatch.setattr("luna.config.secrets.default_store", lambda: store)
    assert main(["--forget-keys"]) == 0
    assert store.values == {}
    err = capsys.readouterr().err
    assert "GROQ_API_KEY" in err and "a-1234" not in err


class FakeKeyring(types.ModuleType):
    def __init__(self):
        super().__init__("keyring")
        self.data = {}
        self.fail = False
        self.errors = types.ModuleType("keyring.errors")
        self.PasswordDeleteError = type("PasswordDeleteError", (Exception,), {})
        self.errors.PasswordDeleteError = self.PasswordDeleteError

    def get_password(self, service, name):
        if self.fail:
            raise RuntimeError("no backend")
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        if self.fail:
            raise RuntimeError("no backend")
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.data:
            raise self.PasswordDeleteError("missing")
        del self.data[(service, name)]


@pytest.fixture
def fake_keyring(monkeypatch):
    mod = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", mod)
    monkeypatch.setitem(sys.modules, "keyring.errors", mod.errors)
    return mod


def test_keyring_store_roundtrip_uses_service_name(fake_keyring):
    store = KeyringStore()
    assert store.get("GROQ_API_KEY") is None
    store.set("GROQ_API_KEY", "v-1234")
    assert fake_keyring.data == {("L.U.N.A.", "GROQ_API_KEY"): "v-1234"}
    assert store.get("GROQ_API_KEY") == "v-1234"
    store.delete("GROQ_API_KEY")
    store.delete("GROQ_API_KEY")  # already gone: no error
    assert store.get("GROQ_API_KEY") is None


def test_keyring_backend_failure(fake_keyring):
    fake_keyring.fail = True
    store = KeyringStore()
    assert store.get("GROQ_API_KEY") is None  # never breaks start
    with pytest.raises(SecretStoreError) as err:
        store.set("GROQ_API_KEY", "secret-9999")
    assert "secret-9999" not in str(err.value)
