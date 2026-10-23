import json
import logging
from typing import Any

import httpx
import pytest
import respx

from luna.core.errors import ProviderRequestError, ProviderUnavailable, RateLimited
from luna.llm.base import LLMProvider, Message, ToolCall, ToolSchema
from luna.llm.openai_compat import MAX_RETRY_AFTER_S, OpenAICompatProvider, Quirks

BASE = "https://llm.example.test/v1"
URL = f"{BASE}/chat/completions"
KEY = "gsk_SUPERSECRETKEY1234567890"

SEARCH_TOOL = ToolSchema(
    "web_search",
    "Search the web",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)


# Building an SSL context per client costs ~1 s; the mocked tests share one (verify is moot).
HTTP = httpx.Client(trust_env=False, verify=False)


def make(quirks: Quirks | None = None, api_key: str | None = KEY) -> OpenAICompatProvider:
    return OpenAICompatProvider("groq", BASE, "test-model", api_key, quirks, http_client=HTTP)


def completion(
    content: str | None = "Hello",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model-0613",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def tool_call(call_id: str | None, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


def ask(provider: OpenAICompatProvider, tools: tuple[ToolSchema, ...] = ()):
    return provider.chat([Message("user", "hi")], tools, timeout_s=5)


def test_satisfies_protocol() -> None:
    assert isinstance(make(), LLMProvider)


@respx.mock
def test_plain_text_completion() -> None:
    respx.post(URL).respond(
        json=completion(
            "Привет!", usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        )
    )
    response = ask(make())
    assert response.text == "Привет!"
    assert response.tool_calls == ()
    assert response.provider == "groq"
    assert response.model == "test-model-0613"
    assert response.finish_reason == "stop"
    assert response.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


@respx.mock
def test_missing_usage_and_null_content() -> None:
    respx.post(URL).respond(json=completion(None, [tool_call("c1", "get_datetime", "{}")]))
    response = ask(make())
    assert response.text is None
    assert response.usage is None


@respx.mock
def test_request_shape_without_tools() -> None:
    route = respx.post(URL).respond(json=completion())
    provider = make(Quirks(supports_tool_choice=True, reasoning_effort="low", max_tokens=256))
    provider.chat([Message("system", "be brief"), Message("user", "hi")], timeout_s=5)
    body = sent(route)
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert "tools" not in body
    assert "tool_choice" not in body
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] == 256
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"


@respx.mock
def test_default_quirks_send_no_optional_params() -> None:
    route = respx.post(URL).respond(json=completion())
    ask(make(), (SEARCH_TOOL,))
    body = sent(route)
    assert "tool_choice" not in body
    assert "reasoning_effort" not in body
    assert "max_tokens" not in body
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web",
                "parameters": SEARCH_TOOL.parameters,
            },
        }
    ]


@respx.mock
def test_tool_choice_only_when_supported_and_tools_given() -> None:
    route = respx.post(URL).respond(json=completion())
    ask(make(Quirks(supports_tool_choice=False)), (SEARCH_TOOL,))
    assert "tool_choice" not in sent(route)
    ask(make(Quirks(supports_tool_choice=True)), (SEARCH_TOOL,))
    assert sent(route)["tool_choice"] == "auto"


@respx.mock
def test_tool_round_trip_messages_are_serialized() -> None:
    route = respx.post(URL).respond(json=completion("Done"))
    call = ToolCall("call_1", "web_search", {"query": "погода"})
    history = [
        Message("user", "weather?"),
        Message("assistant", None, tool_calls=(call,)),
        Message("tool", "sunny", tool_call_id="call_1"),
    ]
    make().chat(history, [SEARCH_TOOL], timeout_s=5)
    messages = sent(route)["messages"]
    assert messages[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "погода"}, ensure_ascii=False),
                },
            }
        ],
    }
    assert messages[2] == {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}


@respx.mock
def test_tool_call_response_with_json_arguments() -> None:
    respx.post(URL).respond(
        json=completion(
            None,
            [
                tool_call("call_a", "web_search", '{"query": "курс доллара"}'),
                tool_call("call_b", "get_datetime", "{}"),
            ],
            finish_reason="tool_calls",
        )
    )
    response = ask(make(), (SEARCH_TOOL,))
    assert response.text is None
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == (
        ToolCall("call_a", "web_search", {"query": "курс доллара"}),
        ToolCall("call_b", "get_datetime", {}),
    )


@respx.mock
@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_arguments_become_empty_dict(raw: str) -> None:
    respx.post(URL).respond(json=completion(None, [tool_call("c1", "get_datetime", raw)]))
    assert ask(make()).tool_calls == (ToolCall("c1", "get_datetime", {}),)


@respx.mock
@pytest.mark.parametrize("raw", ['{"query": ', "not json", "[1, 2]", '"text"'])
def test_invalid_arguments_raise_request_error(raw: str) -> None:
    respx.post(URL).respond(json=completion(None, [tool_call("c1", "web_search", raw)]))
    with pytest.raises(ProviderRequestError):
        ask(make())


@respx.mock
def test_empty_and_duplicate_tool_call_ids_are_replaced() -> None:
    respx.post(URL).respond(
        json=completion(
            None,
            [
                tool_call("dup", "web_search", '{"query": "a"}'),
                tool_call("dup", "web_search", '{"query": "b"}'),
                tool_call("", "get_datetime", "{}"),
                tool_call(None, "get_datetime", "{}"),
            ],
        )
    )
    calls = ask(make()).tool_calls
    ids = [c.id for c in calls]
    assert ids[0] == "dup"
    assert all(ids)
    assert len(set(ids)) == 4
    assert [c.arguments for c in calls[:2]] == [{"query": "a"}, {"query": "b"}]


@respx.mock
def test_no_choices_is_unavailable() -> None:
    respx.post(URL).respond(json={**completion(), "choices": []})
    with pytest.raises(ProviderUnavailable):
        ask(make())


@respx.mock
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("2.5", 2.5),
        (None, None),
        ("soon", None),
        ("-3", 0.0),
        ("1_0", None),
        ("86400", 300.0),
        ("1e9", 300.0),
    ],
)
def test_429_maps_to_rate_limited(header: str | None, expected: float | None) -> None:
    headers = {"Retry-After": header} if header is not None else {}
    respx.post(URL).respond(429, headers=headers, json={"error": {"message": "slow down"}})
    with pytest.raises(RateLimited) as info:
        ask(make())
    assert info.value.retry_after_s == expected


@respx.mock
def test_429_retry_after_http_date() -> None:
    respx.post(URL).respond(
        429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}, json={"error": {}}
    )
    with pytest.raises(RateLimited) as info:
        ask(make())
    assert info.value.retry_after_s is not None and info.value.retry_after_s > 0


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 409])
def test_server_errors_map_to_unavailable(status: int) -> None:
    respx.post(URL).respond(status, json={"error": {"message": "boom"}})
    with pytest.raises(ProviderUnavailable):
        ask(make())


@respx.mock
@pytest.mark.parametrize("status", [400, 404, 422])
def test_client_errors_map_to_request_error(status: int) -> None:
    respx.post(URL).respond(status, json={"error": {"message": "bad schema"}})
    with pytest.raises(ProviderRequestError, match="bad schema"):
        ask(make())


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_do_not_leak_the_key(status: int) -> None:
    respx.post(URL).respond(status, json={"error": {"message": f"Invalid API key: {KEY}"}})
    with pytest.raises(ProviderRequestError) as info:
        ask(make())
    assert KEY not in str(info.value)
    assert KEY not in repr(info.value)
    assert str(status) in str(info.value)


@respx.mock
def test_error_detail_is_redacted_and_truncated() -> None:
    respx.post(URL).respond(400, json={"error": {"message": f"bad {KEY} " + "x" * 1000}})
    with pytest.raises(ProviderRequestError) as info:
        ask(make())
    assert KEY not in str(info.value)
    assert len(str(info.value)) < 400


@respx.mock
def test_timeout_maps_to_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderUnavailable, match="timed out"):
        ask(make())


@respx.mock
def test_connection_error_maps_to_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailable):
        ask(make())


@respx.mock
def test_sdk_does_not_retry() -> None:
    route = respx.post(URL).respond(503, json={"error": {}})
    with pytest.raises(ProviderUnavailable):
        ask(make())
    assert route.call_count == 1


@respx.mock
def test_timeout_is_passed_to_the_request() -> None:
    route = respx.post(URL).respond(json=completion())
    OpenAICompatProvider("g", BASE, "m", KEY, connect_timeout_s=3, http_client=HTTP).chat(
        [Message("user", "hi")], timeout_s=8
    )
    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["read"] == 8
    assert timeout["connect"] == 3
    OpenAICompatProvider("g", BASE, "m", KEY, connect_timeout_s=3, http_client=HTTP).chat(
        [Message("user", "hi")], timeout_s=2
    )
    assert route.calls.last.request.extensions["timeout"]["connect"] == 2


@respx.mock
def test_local_server_without_api_key() -> None:
    route = respx.post("http://localhost:1234/v1/chat/completions").respond(json=completion("ok"))
    provider = OpenAICompatProvider(
        "local", "http://localhost:1234/v1", "qwen3-4b", http_client=HTTP
    )
    assert provider.chat([Message("user", "hi")], timeout_s=5).text == "ok"
    assert route.called


def test_repr_hides_the_key() -> None:
    provider = make()
    assert KEY not in repr(provider)
    assert KEY not in str(provider)


@respx.mock
def test_no_key_in_debug_logs(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(URL).respond(401, json={"error": {"message": "nope"}})
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderRequestError):
        ask(make())
    assert KEY not in caplog.text
    assert KEY not in "".join(r.getMessage() for r in caplog.records)


@respx.mock
def test_retry_after_http_date_far_future_is_capped() -> None:
    respx.post(URL).respond(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}, json={})
    with pytest.raises(RateLimited) as info:
        ask(make())
    assert info.value.retry_after_s == MAX_RETRY_AFTER_S


@respx.mock
def test_non_json_200_is_unavailable_without_body() -> None:
    respx.post(URL).respond(
        200, headers={"content-type": "application/json"}, content=b"<html>SECRET"
    )
    with pytest.raises(ProviderUnavailable, match="malformed") as info:
        ask(make())
    assert "SECRET" not in str(info.value)


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {**completion(), "choices": [{"index": 0, "message": None, "finish_reason": "stop"}]},
        {**completion(), "choices": [{"index": 0, "finish_reason": "stop"}]},
        {**completion(), "choices": "oops"},
        {"unexpected": True},
    ],
)
def test_structurally_broken_completion_is_unavailable(body: dict[str, Any]) -> None:
    respx.post(URL).respond(json=body)
    with pytest.raises(ProviderUnavailable):
        ask(make())


@respx.mock
def test_arguments_given_as_object_are_accepted() -> None:
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "web_search", "arguments": {"query": "x"}},
    }
    respx.post(URL).respond(json=completion(None, [call]))
    assert ask(make()).tool_calls == (ToolCall("c1", "web_search", {"query": "x"}),)


@respx.mock
def test_arguments_of_unsupported_type_are_rejected() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": 5}}
    respx.post(URL).respond(json=completion(None, [call]))
    with pytest.raises(ProviderRequestError):
        ask(make())


@respx.mock
@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [(None, "content_filter"), ("", "stop"), (None, "length"), ([], "stop")],
)
def test_empty_completion_is_unavailable(content: Any, finish_reason: str) -> None:
    respx.post(URL).respond(json=completion(content, finish_reason=finish_reason))
    with pytest.raises(ProviderUnavailable, match=f"empty response.*{finish_reason}"):
        ask(make())


@respx.mock
def test_truncated_tool_arguments_mention_the_token_limit() -> None:
    call = tool_call("c1", "web_search", '{"query": "abc')
    respx.post(URL).respond(json=completion(None, [call], finish_reason="length"))
    with pytest.raises(ProviderRequestError, match="max_tokens"):
        ask(make())


@respx.mock
def test_content_parts_are_joined() -> None:
    parts = [
        {"type": "text", "text": "Hel"},
        {"type": "refusal", "text": "x"},
        {"type": "text", "text": "lo"},
    ]
    respx.post(URL).respond(json=completion(parts))
    assert ask(make()).text == "Hello"


@respx.mock
def test_missing_finish_reason_and_bad_usage() -> None:
    body = completion("hi", usage={"prompt_tokens": "many", "completion_tokens": None})
    del body["choices"][0]["finish_reason"]
    respx.post(URL).respond(json=body)
    response = ask(make())
    assert response.finish_reason is None
    assert response.usage is None


@respx.mock
def test_regenerated_ids_are_sent_back_consistently() -> None:
    route = respx.post(URL)
    route.respond(json=completion(None, [tool_call("", "get_datetime", "{}")]))
    first = ask(make())
    call = first.tool_calls[0]
    assert call.id
    route.respond(json=completion("It is noon"))
    make().chat(
        [
            Message("user", "time?"),
            Message("assistant", first.text, tool_calls=first.tool_calls),
            Message("tool", "12:00", tool_call_id=call.id),
        ],
        timeout_s=5,
    )
    messages = sent(route)["messages"]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == call.id


def test_default_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    provider = OpenAICompatProvider("g", BASE, "m", KEY)
    assert provider._client._client._trust_env is False


def test_sdk_loggers_are_not_verbose() -> None:
    for name in ("openai", "httpx"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
