import datetime as stdlib_datetime
import importlib

from fakes import FakeClock, FakeConfirmer
from luna.tools.base import SafetyLevel, Tool, ToolContext
from luna.tools.builtin.datetime import GetDatetimeTool, current_datetime_context
from luna.tools.schema import check_schema, validate_args

CTX = ToolContext("r1", "text", FakeConfirmer(), "what day is it")
UTC = stdlib_datetime.UTC
PLUS5 = stdlib_datetime.timezone(stdlib_datetime.timedelta(hours=5))
# 2026-10-23 (Friday) 12:34:56 UTC
EPOCH = stdlib_datetime.datetime(2026, 10, 23, 12, 34, 56, tzinfo=UTC).timestamp()


def test_fixed_clock_utc() -> None:
    res = GetDatetimeTool(FakeClock(EPOCH), UTC).run({}, CTX)
    assert res.ok
    assert not res.untrusted
    assert res.data["iso"] == "2026-10-23T12:34:56+00:00"
    assert res.data["date"] == "2026-10-23"
    assert res.data["time"] == "12:34:56"
    assert res.data["weekday"] == "Friday"
    assert res.data["utc_offset"] == "+00:00"
    assert res.summary == "Friday, 2026-10-23 12:34:56 (UTC+00:00)"


def test_offset_shifts_date_and_weekday() -> None:
    res = GetDatetimeTool(FakeClock(EPOCH + 12 * 3600), PLUS5).run({}, CTX)
    assert res.data["iso"] == "2026-10-24T05:34:56+05:00"
    assert res.data["weekday"] == "Saturday"
    assert res.data["utc_offset"] == "+05:00"


def test_negative_offset() -> None:
    tz = stdlib_datetime.timezone(stdlib_datetime.timedelta(hours=-3, minutes=-30))
    res = GetDatetimeTool(FakeClock(EPOCH), tz).run({}, CTX)
    assert res.data["utc_offset"] == "-03:30"
    assert res.data["iso"] == "2026-10-23T09:04:56-03:30"


def test_clock_advances() -> None:
    clock = FakeClock(EPOCH)
    tool = GetDatetimeTool(clock, UTC)
    clock.advance(60)
    assert tool.run({}, CTX).data["time"] == "12:35:56"


def test_default_clock_uses_system_zone() -> None:
    res = GetDatetimeTool().run({}, CTX)
    assert res.ok
    assert stdlib_datetime.datetime.fromisoformat(res.data["iso"]).utcoffset() is not None


def test_context_line() -> None:
    line = current_datetime_context(FakeClock(EPOCH), PLUS5)
    assert line == "Current local date and time: 2026-10-23 17:34:56 (UTC+05:00), Friday."


def test_schema_has_no_required_parameters() -> None:
    tool = GetDatetimeTool()
    assert isinstance(tool, Tool)
    assert tool.safety is SafetyLevel.SAFE
    check_schema(tool.parameters)
    assert "required" not in tool.parameters
    assert validate_args({}, tool.parameters) == []
    assert validate_args({"x": 1}, tool.parameters) != []


def test_stdlib_datetime_not_shadowed() -> None:
    import luna.tools.builtin.datetime as ours

    stdlib = importlib.import_module("datetime")
    assert ours is not stdlib
    assert stdlib is stdlib_datetime
    assert hasattr(stdlib, "datetime")
