"""Negative tests for the fail-closed registry (review findings on PR #32)."""

import json
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeConfirmer, FakeTool
from luna.core.errors import ToolError
from luna.llm.base import ToolCall
from luna.logs.action_log import ActionLog
from luna.tools.base import SafetyLevel, ToolContext, ToolResult
from luna.tools.registry import ToolRegistry


@pytest.fixture
def log(tmp_path: Path) -> ActionLog:
    return ActionLog(tmp_path / "actions.jsonl")


@pytest.fixture
def registry(log: ActionLog) -> ToolRegistry:
    return ToolRegistry(log)


def lines(log: ActionLog) -> list[dict[str, Any]]:
    path = log.current_path()
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def ctx(confirmer: Any = None, text: str = "do it") -> ToolContext:
    return ToolContext("r1", "text", confirmer or FakeConfirmer(), text, "router")


def call(_tool: str = "fake_tool", **args: Any) -> ToolCall:
    return ToolCall(id="c1", name=_tool, arguments=args)


@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "object", "properties": {"a": {"type": "strng"}}},
        {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x$"}}},
        {"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}]}}},
        {"type": "object", "properties": {"a": {"$ref": "#/x"}}},
        {"type": "object", "oneOf": []},
        {"type": "object", "properties": {"a": {"type": "string", "format": "uri"}}},
        {"type": "object", "properties": {"a": {"type": "string", "minLength": "3"}}},
        {"type": "object", "properties": {"a": {"enum": "abc"}}},
        {"type": "object", "properties": {"a": {"enum": []}}},
        {"type": "object", "required": "name"},
        {"type": "object", "properties": []},
        {"type": "object", "additionalProperties": "no"},
        {"type": "object", "properties": {"a": {"type": "number", "maximum": float("nan")}}},
        {"type": "string"},
        {"properties": {}},
        "not a dict",
    ],
)
def test_register_rejects_bad_schemas(registry: ToolRegistry, parameters: Any) -> None:
    with pytest.raises(ToolError):
        registry.register(FakeTool(parameters=parameters))
    assert registry.schemas() == []


@pytest.mark.parametrize(
    "tool",
    [
        FakeTool(name=""),
        FakeTool(name="bad name!"),
        FakeTool(name="x" * 65),
        FakeTool(safety="safe"),  # type: ignore[arg-type]
    ],
)
def test_register_validates_tool_fields(registry: ToolRegistry, tool: FakeTool) -> None:
    with pytest.raises(ToolError):
        registry.register(tool)


def test_nested_objects_reject_extra_fields_by_default(registry: ToolRegistry) -> None:
    schema = {"type": "object", "properties": {"o": {"type": "object"}}}
    registry.register(FakeTool(parameters=schema))
    assert not registry.execute(call(o={"x": 1}), ctx()).ok
    assert registry.execute(call(o={}), ctx()).ok


def test_root_extra_fields_rejected_by_default(registry: ToolRegistry) -> None:
    registry.register(FakeTool())
    assert not registry.execute(call(x=1), ctx()).ok


def test_additional_properties_true_allows_extras(registry: ToolRegistry) -> None:
    registry.register(FakeTool(parameters={"type": "object", "additionalProperties": True}))
    assert registry.execute(call(anything=1), ctx()).ok


def test_enum_and_const_are_type_strict(registry: ToolRegistry) -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"enum": [1]}, "b": {"enum": [True]}, "c": {"const": 0}},
    }
    registry.register(FakeTool(parameters=schema))
    assert not registry.execute(call(a=True), ctx()).ok
    assert not registry.execute(call(b=1), ctx()).ok
    assert not registry.execute(call(c=False), ctx()).ok
    assert registry.execute(call(a=1, b=True, c=0), ctx()).ok


def test_nan_and_infinity_rejected(registry: ToolRegistry) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "number", "maximum": 10}}}
    tool = FakeTool(parameters=schema)
    registry.register(tool)
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert not registry.execute(call(x=bad), ctx()).ok
    assert tool.calls == []


def test_snapshot_is_immutable(registry: ToolRegistry) -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}
    tool = FakeTool(parameters=schema, safety=SafetyLevel.SAFE)
    registry.register(tool)
    schema["properties"]["a"] = {"type": "integer"}
    tool.safety = SafetyLevel.DANGEROUS
    assert not registry.execute(call(a=1), ctx()).ok
    assert registry.execute(call(a="s"), ctx()).ok  # still SAFE: no confirmation needed
    registry.schemas()[0].parameters["properties"]["a"] = {}
    assert registry.schemas()[0].parameters["properties"]["a"] == {"type": "string"}


def test_tool_receives_copy_of_args(registry: ToolRegistry) -> None:
    class Mutator(FakeTool):
        def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            args["a"] = "changed"
            return self.result

    registry.register(Mutator(parameters={"type": "object", "additionalProperties": True}))
    c = call(a="orig")
    registry.execute(c, ctx())
    assert c.arguments == {"a": "orig"}


def test_confirm_requires_exactly_true(registry: ToolRegistry) -> None:
    class Truthy:
        def confirm(self, prompt: str) -> bool:
            return "yes"  # type: ignore[return-value]

    tool = FakeTool(safety=SafetyLevel.SENSITIVE)
    registry.register(tool)
    assert not registry.execute(call(), ctx(Truthy())).ok
    assert tool.calls == []


def test_prompt_has_full_name_and_args(registry: ToolRegistry) -> None:
    conf = FakeConfirmer()
    schema = {"type": "object", "properties": {"url": {"type": "string"}}}
    registry.register(FakeTool(name="open_site", parameters=schema, safety=SafetyLevel.SENSITIVE))
    url = "https://example.com/" + "a  b" * 60
    assert registry.execute(call("open_site", url=url), ctx(conf)).ok
    assert "open_site" in conf.prompts[0]
    assert json.dumps(url) in conf.prompts[0]


def test_oversized_prompt_refused_not_truncated(registry: ToolRegistry, log: ActionLog) -> None:
    conf = FakeConfirmer()
    schema = {"type": "object", "properties": {"t": {"type": "string"}}}
    tool = FakeTool(parameters=schema, safety=SafetyLevel.SENSITIVE)
    registry.register(tool)
    res = registry.execute(call(t="x" * 3000), ctx(conf))
    assert not res.ok
    assert "too large to confirm" in res.summary
    assert conf.prompts == []
    assert tool.calls == []
    assert lines(log)[0]["confirmed"] is False


def test_args_limits(registry: ToolRegistry, log: ActionLog) -> None:
    tool = FakeTool(parameters={"type": "object", "additionalProperties": True})
    registry.register(tool)
    deep: dict[str, Any] = {}
    cur = deep
    for _ in range(12):
        cur["n"] = {}
        cur = cur["n"]
    assert not registry.execute(call(**deep), ctx()).ok
    assert not registry.execute(call(big="x" * 20000), ctx()).ok
    assert not registry.execute(call(obj=object()), ctx()).ok
    assert tool.calls == []
    assert len(lines(log)) == 3


def test_log_text_truncated(registry: ToolRegistry, log: ActionLog) -> None:
    registry.register(FakeTool(result=ToolResult("fake_tool", True, "s" * 2000)))
    registry.execute(call(), ctx(text="u" * 2000))
    rec = lines(log)[0]
    assert len(rec["summary"]) < 600
    assert rec["summary"].endswith("[truncated]")
    assert len(rec["text"]) < 600


def test_base_exception_writes_record_and_propagates(
    registry: ToolRegistry, log: ActionLog
) -> None:
    registry.register(FakeTool(error=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        registry.execute(call(), ctx())
    [rec] = lines(log)
    assert rec["ok"] is False


def test_garbage_call_never_raises(registry: ToolRegistry, log: ActionLog) -> None:
    registry.register(FakeTool())
    for bad in (ToolCall("c", None, {}), ToolCall("c", "fake_tool", None)):  # type: ignore[arg-type]
        assert not registry.execute(bad, ctx()).ok
    assert len(lines(log)) == 2


def test_log_raising_does_not_fail_call() -> None:
    class BadLog:
        def write(self, record: Any) -> bool:
            raise RuntimeError("disk")

    reg = ToolRegistry(BadLog())  # type: ignore[arg-type]
    reg.register(FakeTool())
    assert reg.execute(call(), ctx()).ok
