"""Keys dialog logic (no Qt): Test results via respx, save, status, auto-show condition."""

import httpx
import pytest
import respx

from luna.config.secrets import InMemoryStore
from luna.config.settings import Settings
from luna.logs.redact import clear_secrets, redact_text
from luna.ui.shell.keys import KeyCheck, KeysPresenter, check_key, has_cloud_llm_key, key_fields

URL = "https://api.groq.com/openai/v1/models"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_secrets()
    yield
    clear_secrets()


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, KeyCheck.OK),
        (401, KeyCheck.REJECTED),
        (403, KeyCheck.REJECTED),
        (500, KeyCheck.ERROR),
    ],
)
def test_check_key_status(status, expected):
    route = respx.get(URL).respond(status)
    assert check_key(URL, " k-12345678 ") == expected
    assert route.calls.last.request.headers["authorization"] == "Bearer k-12345678"


@respx.mock
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ConnectError("c")])
def test_check_key_no_connection(exc):
    respx.get(URL).mock(side_effect=exc)
    assert check_key(URL, "k-12345678") == KeyCheck.NO_CONNECTION


def test_tested_key_is_registered_for_redaction():
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    check_key(URL, "typed-secret-key", transport=transport)
    assert "typed-secret-key" not in redact_text("x typed-secret-key y")


def test_fields_use_config_urls():
    fields = {f.env_name: f for f in key_fields(Settings())}
    assert fields["GROQ_API_KEY"].models_url == URL
    assert fields["GROQ_API_KEY"].required
    assert fields["GEMINI_API_KEY"].models_url.endswith("/v1beta/openai/models")
    assert not fields["GEMINI_API_KEY"].required


def test_has_cloud_llm_key():
    assert not has_cloud_llm_key(Settings())
    assert has_cloud_llm_key(Settings().with_api_keys({"GEMINI_API_KEY": "g-12345"}))


def test_save_status_and_redaction():
    store = InMemoryStore()
    presenter = KeysPresenter(Settings().with_api_keys({}), store)
    groq = presenter.fields[0]
    assert presenter.status(groq) == "not set"
    assert not presenter.save(groq, "   ")
    assert store.values == {}
    assert presenter.save(groq, " gk-saved-value ")
    assert store.values == {"GROQ_API_KEY": "gk-saved-value"}
    assert presenter.status(groq) == "saved"
    assert "gk-saved-value" not in redact_text("gk-saved-value")


def test_status_env_override():
    settings = Settings().with_api_keys({"GROQ_API_KEY": "env-12345"})
    presenter = KeysPresenter(settings, InMemoryStore({"GROQ_API_KEY": "saved-1234"}))
    assert "overrides" in presenter.status(presenter.fields[0])
    presenter2 = KeysPresenter(settings, InMemoryStore())
    assert presenter2.status(presenter2.fields[0]) == "set in environment / .env"
