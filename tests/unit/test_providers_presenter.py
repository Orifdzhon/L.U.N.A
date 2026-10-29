"""Providers dialog logic (no Qt): the provider check via MockTransport and the presenter."""

import json
import logging

import httpx
import pytest

from luna.config.loader import load_settings
from luna.config.secrets import InMemoryStore
from luna.config.settings import Settings
from luna.core.errors import ConfigError
from luna.llm.presets import load_presets
from luna.logs.redact import clear_secrets, redact_text
from luna.ui.shell.keys import KeyCheck, has_cloud_llm_key
from luna.ui.shell.providers import (
    ProviderCheck,
    ProvidersPresenter,
    ToolSupport,
    check_provider,
)
from luna.ui.shell.strings import t

BASE = "https://llm.example.com/v1"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_secrets()
    yield
    clear_secrets()


def transport(models=None, chat=None, seen=None):
    """``models``/``chat``: an httpx.Response, an exception, or None (404)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        answer = models if request.method == "GET" else chat
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else httpx.Response(404)

    return httpx.MockTransport(handler)


def tool_call_answer():
    message = {"tool_calls": [{"id": "1", "type": "function", "function": {"name": "ping"}}]}
    return httpx.Response(200, json={"choices": [{"message": message}]})


def plain_answer():
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


def listing(*ids):
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]})


def check(models, chat, key="k-secret-1234", model="m1", seen=None):
    return check_provider(BASE, model, key, transport=transport(models, chat, seen))


def test_key_ok_and_tools_yes_with_latency_and_models():
    seen = []
    result = check(listing("b-model", "a-model"), tool_call_answer(), seen=seen)
    assert result.key == KeyCheck.OK
    assert result.tools == ToolSupport.YES
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.models == ("a-model", "b-model")
    assert seen[0].url == f"{BASE}/models"
    assert seen[0].headers["authorization"] == "Bearer k-secret-1234"
    body = json.loads(seen[1].content)
    assert body["max_tokens"] == 32
    assert body["tools"][0]["function"]["name"] == "ping"
    assert body["messages"][0]["content"] == "Call the ping tool."
    assert {r.url.host for r in seen} == {"llm.example.com"}  # the key goes only to this host


def test_tools_unclear_when_answered_without_call():
    assert check(listing("m1"), plain_answer()).tools == ToolSupport.UNCLEAR


@pytest.mark.parametrize("text", ["tools are not supported", "Function calling unsupported"])
@pytest.mark.parametrize("status", [400, 422])
def test_tools_no_on_rejection_mentioning_tools(status, text):
    result = check(listing("m1"), httpx.Response(status, text=text))
    assert result.tools == ToolSupport.NO
    assert result.latency_ms is None or result.latency_ms >= 0


def test_unrelated_400_is_not_a_tools_verdict():
    assert check(listing("m1"), httpx.Response(400, text="model not found")).tools is None
    # mentions tools but does not say they are unsupported: not a verdict either
    assert check(listing("m1"), httpx.Response(400, text="tools[0].name is invalid")).tools is None


@pytest.mark.parametrize("status", [401, 403])
def test_key_rejected_skips_the_probe(status):
    seen = []
    result = check(httpx.Response(status), tool_call_answer(), seen=seen)
    assert result == ProviderCheck(KeyCheck.REJECTED)
    assert len(seen) == 1


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ConnectError("c")])
def test_no_connection(exc):
    assert check(exc, None).key == KeyCheck.NO_CONNECTION
    assert check(listing("m1"), exc).key == KeyCheck.OK  # probe failed: key is still fine
    assert check(listing("m1"), exc).tools is None


def test_unexpected_models_status_is_error_but_missing_models_endpoint_is_fine():
    assert check(httpx.Response(500), None).key == KeyCheck.ERROR
    assert check(None, tool_call_answer()).tools == ToolSupport.YES  # 404 on /models


def test_models_only_when_model_blank():
    seen = []
    result = check(listing("x"), tool_call_answer(), model="  ", seen=seen)
    assert result.models == ("x",) and result.tools is None
    assert len(seen) == 1


def test_openrouter_models_filtered_to_free_with_tools():
    payload = {
        "data": [
            {"id": "a/free-tools:free", "supported_parameters": ["tools", "temperature"]},
            {"id": "b/free-notools:free", "supported_parameters": ["temperature"]},
            {"id": "c/paid-tools", "supported_parameters": ["tools"]},
            {"id": "d/free-unknown:free"},
        ]
    }
    result = check_provider(
        "https://openrouter.ai/api/v1",
        "",
        "k-12345678",
        transport=transport(httpx.Response(200, json=payload)),
    )
    assert result.models == ("a/free-tools:free",)


def test_invalid_url_and_garbage_json():
    assert check_provider("http://evil.example/v1", "m", "k-12345678").key == KeyCheck.ERROR
    assert check(httpx.Response(200, text="not json"), plain_answer()).models == ()


def test_keyless_local_sends_no_authorization(caplog):
    seen = []
    with caplog.at_level(logging.DEBUG):
        check_provider(
            "http://localhost:1234/v1",
            "m",
            None,
            transport=transport(listing("m"), plain_answer(), seen),
        )
    assert all("authorization" not in r.headers for r in seen)


def test_tested_key_is_registered_for_redaction():
    check(listing("m1"), plain_answer(), key="typed-secret-key")
    assert "typed-secret-key" not in redact_text("x typed-secret-key y")


# --- presenter --------------------------------------------------------------------------------


def make(tmp_path, settings=None, store=None):
    store = store if store is not None else InMemoryStore()
    settings = settings or Settings().with_api_keys({})
    return ProvidersPresenter(settings, store, load_presets(), tmp_path / "providers.toml"), store


def names(p):
    return [d.name for d in p.drafts]


def test_initial_list_is_chain_then_disabled(tmp_path):
    settings = Settings()
    settings.llm.chain = ["gemini", "groq"]
    p, _ = make(tmp_path, settings)
    assert names(p) == ["gemini", "groq", "local"]
    assert [d.enabled for d in p.drafts] == [True, True, False]
    assert p.badge(0) == "бесплатно" and p.badge(2) == "локально"


def test_move_enable_remove(tmp_path):
    p, _ = make(tmp_path)
    assert p.move(0, -1) == 0
    assert p.move(0, 1) == 1 and names(p)[:2] == ["gemini", "groq"]
    p.set_enabled(0, False)
    p.remove(2)
    assert names(p) == ["gemini", "groq"]
    assert [d.enabled for d in p.drafts] == [False, True]


def test_add_from_preset_uses_conventional_key_name_and_unique_name(tmp_path):
    p, _ = make(tmp_path)
    by_id = {x.id: x for x in p.presets}
    i = p.add_from_preset(by_id["openrouter"])
    draft = p.drafts[i]
    assert draft.name == "openrouter" and draft.conf.api_key_env == "OPENROUTER_API_KEY"
    assert draft.conf.model == "" and p.badge(i) == "бесплатно"
    j = p.add_from_preset(by_id["groq"])
    assert p.drafts[j].name == "groq_2"
    assert p.drafts[j].conf.reasoning_effort == "low"
    assert p.badge(j) == "бесплатно" and "speech recognition" in p.note(j)


def test_add_custom(tmp_path):
    p, _ = make(tmp_path)
    i = p.add_custom("My_Srv", f"{BASE}", "big")
    assert p.drafts[i].conf.api_key_env == "LUNA_MY_SRV_API_KEY"
    assert p.drafts[i].custom and p.badge(i) == ""
    j = p.add_custom("lan", "http://localhost:8080/v1", "m")
    assert p.drafts[j].conf.api_key_env is None and not p.needs_key(j)
    for bad_name in ("", "1abc", "has space", "x" * 33, "groq"):
        with pytest.raises(ValueError):
            p.add_custom(bad_name, BASE, "m")
    with pytest.raises(ValueError, match="https"):
        p.add_custom("bad", "http://example.com/v1", "m")


def test_set_base_url_only_for_custom_and_resets_tools(tmp_path):
    p, _ = make(tmp_path)
    with pytest.raises(ValueError, match="нельзя"):
        p.set_base_url(0, BASE)
    i = p.add_custom("mine", BASE, "m")
    p.drafts[i].conf.supports_tools = True
    p.set_base_url(i, "https://other.example/v1")
    assert p.drafts[i].conf.base_url == "https://other.example/v1"
    assert p.drafts[i].conf.supports_tools is None
    with pytest.raises(ValueError):
        p.set_base_url(i, "http://other.example/v1")


def test_key_status_texts(tmp_path):
    p, store = make(tmp_path)
    assert p.key_status(0) == t("providers.key.not_set", "ru")
    assert p.key_status(2) == t("providers.key.none", "ru")
    store.set("GROQ_API_KEY", "gk-1234")
    assert p.key_status(0) == t("providers.key.saved", "ru")
    settings = Settings().with_api_keys({"GROQ_API_KEY": "env-12345"})
    p2, _ = make(tmp_path, settings, InMemoryStore({"GROQ_API_KEY": "saved-1234"}))
    assert t("providers.key.overridden", "ru") == p2.key_status(0)
    p3, _ = make(tmp_path, settings)
    assert p3.key_status(0) == t("providers.key.env", "ru")


def test_save_key_and_redaction(tmp_path):
    p, store = make(tmp_path)
    assert not p.save_key(0, "   ")
    assert not p.save_key(2, "ignored-1234")  # local provider: no key name
    assert p.save_key(0, " gk-saved-value ")
    assert store.values == {"GROQ_API_KEY": "gk-saved-value"}
    assert p.key_status(0) == t("providers.key.saved", "ru")
    assert "gk-saved-value" not in redact_text("gk-saved-value")
    assert p.key_for_test(0, "") == "gk-saved-value"
    assert p.key_for_test(0, " typed ") == "typed"
    assert p.key_for_test(2, "") is None


def test_record_check_sets_supports_tools_and_warnings(tmp_path):
    p, _ = make(tmp_path)
    assert any("не проверена" in w for w in p.warnings(0))
    p.record_check(0, ProviderCheck(KeyCheck.OK, ToolSupport.NO))
    assert p.drafts[0].conf.supports_tools is False and t(
        "providers.warn.no_tools", "ru"
    ) in p.warnings(0)
    p.record_check(0, ProviderCheck(KeyCheck.OK, ToolSupport.YES))
    assert p.drafts[0].conf.supports_tools is True and p.warnings(0) == []
    p.record_check(0, ProviderCheck(KeyCheck.OK, ToolSupport.UNCLEAR))
    assert p.drafts[0].conf.supports_tools is None
    p.record_check(0, ProviderCheck(KeyCheck.NO_CONNECTION))  # nothing learned: unchanged
    assert p.drafts[0].conf.supports_tools is None


def test_paid_and_trial_warnings(tmp_path):
    p, _ = make(tmp_path)
    by_id = {x.id: x for x in p.presets}
    paid = p.add_from_preset(by_id["deepseek"])
    trial = p.add_from_preset(by_id["qwen"])
    assert p.badge(paid) == "платно" and any("Платный" in w for w in p.warnings(paid))
    assert p.badge(trial) == "пробный период" and any("Пробный" in w for w in p.warnings(trial))


def test_save_writes_overlay_that_the_loader_reads_back(tmp_path):
    env = {"APPDATA": str(tmp_path)}
    settings = load_settings(env=env, cwd=tmp_path, store=InMemoryStore())
    store = InMemoryStore()
    p = ProvidersPresenter(settings, store, load_presets(), tmp_path / "LUNA" / "providers.toml")
    p.add_custom("mine", BASE, "big")
    p.move(3, -1)
    p.move(2, -1)
    p.move(1, -1)
    p.move(0, -1)
    p.record_check(0, ProviderCheck(KeyCheck.OK, ToolSupport.NO))
    assert p.save() == tmp_path / "LUNA" / "providers.toml"
    assert (tmp_path / "LUNA" / "providers.toml").is_file()
    again = load_settings(env=env, cwd=tmp_path, store=InMemoryStore())
    assert again.llm.chain[0] == "mine"
    assert again.llm.providers["mine"].supports_tools is False
    assert "big" in (tmp_path / "LUNA" / "providers.toml").read_text(encoding="utf-8")


def test_save_rejects_empty_chain_and_missing_model(tmp_path):
    p, _ = make(tmp_path)
    for i in range(len(p.drafts)):
        p.set_enabled(i, False)
    with pytest.raises(ConfigError, match="хотя бы"):
        p.save()
    by_id = {x.id: x for x in p.presets}
    i = p.add_from_preset(by_id["openrouter"])  # no default model
    with pytest.raises(ConfigError, match="не указана модель"):
        p.save()
    p.set_model(i, "x/y:free")
    p.save()
    assert (tmp_path / "providers.toml").is_file()


def test_has_cloud_llm_key():
    assert not has_cloud_llm_key(Settings())
    assert has_cloud_llm_key(Settings().with_api_keys({"GEMINI_API_KEY": "g-12345"}))


def test_apply_check_drops_stale_results(tmp_path):
    p, _ = make(tmp_path)
    i = p.add_custom("mine", BASE, "m1")
    result = ProviderCheck(KeyCheck.OK, ToolSupport.NO)
    assert p.apply_check("gone", BASE, "m1", result) is None
    assert p.apply_check("mine", "https://other.example/v1", "m1", result) is None
    assert p.apply_check("mine", BASE, "other-model", result) is None
    assert p.drafts[i].conf.supports_tools is None
    assert p.apply_check("mine", BASE, "m1", result) == i
    assert p.drafts[i].conf.supports_tools is False
    assert p.apply_check("mine", BASE, "", ProviderCheck(KeyCheck.OK)) == i  # fetch-only
