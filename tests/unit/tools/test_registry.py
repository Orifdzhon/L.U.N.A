import json
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeConfirmer, FakeTool
from luna.core.errors import ToolError
from luna.llm.base import ToolCall
from luna.logs.action_log import ActionLog
from luna.logs.redact import clear_secrets, register_secret
from luna.tools.base import SafetyLevel, ToolContext, ToolResult
from luna.tools.registry import ToolRegistry
from luna.tools.schema import validate_args

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["name"],
    "additionalProperties": False,
}


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


def ctx(confirmer: FakeConfirmer | None = None) -> ToolContext:
    return ToolContext(
        request_id="r1",
        source="text",
        confirmer=confirmer or FakeConfirmer(),
        user_text="do it",
        provider="router",
    )


def call(_tool: str = "fake_tool", **args: Any) -> ToolCall:
    return ToolCall(id="c1", name=_tool, arguments=args)


def test_register_refuses_dangerous_and_duplicates(registry: ToolRegistry) -> None:
    with pytest.raises(ToolError):
        registry.register(FakeTool(safety=SafetyLevel.DANGEROUS))
    registry.register(FakeTool())
    with pytest.raises(ToolError):
        registry.register(FakeTool())


def test_dangerous_allowed_with_flag_still_confirms(log: ActionLog) -> None:
    reg = ToolRegistry(log, allow_dangerous=True)
    tool = FakeTool(safety=SafetyLevel.DANGEROUS)
    reg.register(tool)
    res = reg.execute(call(), ctx(FakeConfirmer(False)))
    assert not res.ok
    assert tool.calls == []
    assert lines(log)[0]["confirmed"] is False


def test_schemas(registry: ToolRegistry) -> None:
    registry.register(FakeTool(name="a", parameters=SCHEMA))
    [s] = registry.schemas()
    assert (s.name, s.parameters) == ("a", SCHEMA)


def test_safe_tool_runs_and_logs(registry: ToolRegistry, log: ActionLog) -> None:
    tool = FakeTool(parameters=SCHEMA)
    registry.register(tool)
    res = registry.execute(call(name="x", n=2), ctx())
    assert res.ok
    assert tool.calls[0][0] == {"name": "x", "n": 2}
    [rec] = lines(log)
    assert rec["tool"] == "fake_tool"
    assert rec["args"] == {"name": "x", "n": 2}
    assert rec["safety"] == "safe"
    assert rec["confirmed"] is None
    assert rec["ok"] is True
    assert rec["provider"] == "router"
    assert isinstance(rec["duration_ms"], int)


def test_unknown_tool(registry: ToolRegistry, log: ActionLog) -> None:
    res = registry.execute(call("nope"), ctx())
    assert not res.ok
    assert "unknown tool" in res.summary
    [rec] = lines(log)
    assert rec["tool"] == "nope"
    assert rec["ok"] is False
    assert rec["safety"] == "unknown"


@pytest.mark.parametrize(
    "args",
    [{}, {"name": 5}, {"name": "x", "n": "1"}, {"name": "x", "n": True}, {"name": "x", "z": 1}],
)
def test_invalid_args_do_not_run_tool(registry: ToolRegistry, log: ActionLog, args: Any) -> None:
    tool = FakeTool(parameters=SCHEMA)
    registry.register(tool)
    res = registry.execute(call(**args), ctx())
    assert not res.ok
    assert res.summary.startswith("invalid arguments")
    assert tool.calls == []
    assert len(lines(log)) == 1


def test_invalid_args_skip_confirmation(registry: ToolRegistry) -> None:
    conf = FakeConfirmer()
    registry.register(FakeTool(parameters=SCHEMA, safety=SafetyLevel.SENSITIVE))
    registry.execute(call(), ctx(conf))
    assert conf.prompts == []


def test_non_dict_arguments(registry: ToolRegistry, log: ActionLog) -> None:
    registry.register(FakeTool(parameters=SCHEMA))
    res = registry.execute(ToolCall("c", "fake_tool", ["x"]), ctx())  # type: ignore[arg-type]
    assert not res.ok
    assert lines(log)[0]["args"] == {}


@pytest.mark.parametrize(("answer", "runs"), [(True, 1), (False, 0)])
def test_sensitive_confirmation(
    registry: ToolRegistry, log: ActionLog, answer: bool, runs: int
) -> None:
    tool = FakeTool(safety=SafetyLevel.SENSITIVE)
    registry.register(tool)
    conf = FakeConfirmer(answer)
    res = registry.execute(call(), ctx(conf))
    assert res.ok is answer
    assert len(tool.calls) == runs
    assert len(conf.prompts) == 1
    [rec] = lines(log)
    assert rec["confirmed"] is answer
    assert rec["safety"] == "sensitive"


def test_confirmer_exception_counts_as_refusal(registry: ToolRegistry) -> None:
    class Boom:
        def confirm(self, prompt: str) -> bool:
            raise RuntimeError("ui died")

    tool = FakeTool(safety=SafetyLevel.SENSITIVE)
    registry.register(tool)
    c = ToolContext("r", "text", Boom(), "t")
    assert not registry.execute(call(), c).ok
    assert tool.calls == []


def test_unexpected_exception_is_contained(registry: ToolRegistry, log: ActionLog) -> None:
    clear_secrets()
    register_secret("supersecretvalue123")
    try:
        registry.register(FakeTool(error=RuntimeError("key supersecretvalue123 bad")))
        res = registry.execute(call(), ctx())
    finally:
        clear_secrets()
    assert not res.ok
    assert "supersecretvalue123" not in res.summary
    assert "RuntimeError" in res.summary
    assert len(lines(log)) == 1
    assert lines(log)[0]["ok"] is False


def test_tool_error_summary_is_short_and_redacted(registry: ToolRegistry, log: ActionLog) -> None:
    key = "sk-" + "a" * 30
    registry.register(FakeTool(error=ToolError(f"bad {key} " + "x" * 500)))
    res = registry.execute(call(), ctx())
    assert not res.ok
    assert key not in res.summary
    assert len(res.summary) <= 230
    assert key not in log.current_path().read_text(encoding="utf-8")


def test_args_are_redacted_in_log(registry: ToolRegistry, log: ActionLog) -> None:
    key = "sk-" + "b" * 30
    registry.register(FakeTool())
    registry.execute(call(query=key), ctx())
    text = log.current_path().read_text(encoding="utf-8")
    assert key not in text
    assert "***" in text


def test_untrusted_flag_preserved(registry: ToolRegistry) -> None:
    res = ToolResult("fake_tool", True, "page", untrusted=True)
    registry.register(FakeTool(result=res))
    assert registry.execute(call(), ctx()).untrusted is True


def test_invalid_tool_result(registry: ToolRegistry) -> None:
    tool = FakeTool()
    tool.result = "oops"  # type: ignore[assignment]
    registry.register(tool)
    assert not registry.execute(call(), ctx()).ok


def test_log_failure_does_not_fail_call(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    reg = ToolRegistry(ActionLog(blocker / "sub" / "actions.jsonl"))
    reg.register(FakeTool())
    assert reg.execute(call(), ctx()).ok


def test_duration_uses_timer(log: ActionLog) -> None:
    ticks = iter([1.0, 1.25])
    reg = ToolRegistry(log, timer=lambda: next(ticks))
    reg.register(FakeTool())
    reg.execute(call(), ctx())
    assert lines(log)[0]["duration_ms"] == 250


def test_validator_basics() -> None:
    nested = {
        "type": "object",
        "properties": {"a": {"type": "array", "items": {"type": "integer"}, "maxItems": 2}},
    }
    assert validate_args({"a": [1, 2]}, nested) == []
    assert validate_args({"a": [1, "x"]}, nested)
    assert validate_args("x", {"enum": ["a", "b"]})
    assert validate_args(3, {"type": "number", "maximum": 2})
    assert validate_args(None, {"type": ["string", "null"]}) == []
    assert validate_args("", {"type": "string", "minLength": 1})
