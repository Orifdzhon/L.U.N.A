import pytest

from luna.core.clock import WallClock
from luna.core.models import Request
from luna.core.router import Router, parse_datetime_question
from luna.logs.action_log import ActionLog
from luna.tools.builtin.datetime import GetDatetimeTool
from luna.tools.registry import ToolRegistry

# 2026-10-23 15:04:05 UTC, a Friday
EPOCH = 1792767845.0


def make_router(tmp_path, clock: WallClock = lambda: EPOCH) -> Router:
    import datetime as dt

    registry = ToolRegistry(ActionLog(tmp_path / "actions.jsonl"))
    registry.register(GetDatetimeTool(clock, dt.UTC))
    return Router(registry)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("сколько времени", "time"),
        ("Сколько сейчас времени?", "time"),
        ("Луна, который час?", "time"),
        ("сколько времени, пожалуйста", "time"),
        ("какое сейчас время", "time"),
        ("what time is it", "time"),
        ("Luna, what's the time?", "time"),
        ("какое сегодня число?", "date"),
        ("какой сегодня день", "date"),
        ("какая сегодня дата", "date"),
        ("what's the date today", "date"),
        ("what day is it today?", "date"),
    ],
)
def test_parse_positive(text, kind):
    assert parse_datetime_question(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "сколько времени лететь до Москвы",
        "сколько времени займёт дорога домой",
        "какое время года лучше для отпуска",
        "what time is the meeting with Bob",
        "какой день недели был 1 января 2000 года",
        "открой часы",
        "",
    ],
)
def test_parse_negative(text):
    assert parse_datetime_question(text) is None


def test_ru_time_reply(tmp_path):
    result = make_router(tmp_path).route(Request("сколько сейчас времени?", "text"))
    assert result.handled
    assert result.reply.text == "Сейчас 15:04, пятница, 23 октября."
    assert result.reply.provider == "router"
    assert result.reply.actions[0].tool == "get_datetime"


def test_en_time_and_date_reply(tmp_path):
    router = make_router(tmp_path)
    assert router.route(Request("what time is it?", "text")).reply.text == (
        "It's 15:04, Friday, October 23."
    )
    assert router.route(Request("what day is it today", "text")).reply.text == (
        "Today is Friday, October 23."
    )


def test_ru_date_reply(tmp_path):
    reply = make_router(tmp_path).route(Request("какое сегодня число?", "text")).reply
    assert reply.text == "Сегодня пятница, 23 октября."


def test_other_time_context_goes_to_llm(tmp_path):
    result = make_router(tmp_path).route(Request("сколько времени лететь до Москвы", "text"))
    assert not result.handled


def test_missing_tool_falls_through(tmp_path):
    router = Router(ToolRegistry(ActionLog(tmp_path / "a.jsonl")))
    assert not router.route(Request("сколько времени?", "text")).handled
