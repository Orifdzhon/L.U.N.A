import json
import logging
import shutil
import threading
from datetime import datetime

import pytest

from luna.logs.action_log import ActionLog, ActionRecord
from luna.logs.redact import MASK, register_secret

FIELDS = [
    "ts", "request_id", "source", "text", "tool", "args", "safety",
    "confirmed", "ok", "summary", "duration_ms", "provider",
]  # fmt: skip


def make_record(**over):
    base = {
        "request_id": "r1",
        "source": "text",
        "text": "открой калькулятор",
        "tool": "open_app",
        "args": {"name": "Калькулятор"},
        "safety": "safe",
        "ok": True,
        "summary": "Открыт Калькулятор",
        "duration_ms": 12,
        "provider": "router",
    }
    return ActionRecord(**(base | over))


def read_lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_write_appends_one_valid_json_line_with_all_fields(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl")
    log.write(make_record())
    lines = read_lines(log.current_path())
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert list(data) == FIELDS
    assert data["confirmed"] is None
    assert data["tool"] == "open_app"
    datetime.fromisoformat(data["ts"])


def test_ts_and_file_month_come_from_the_same_clock_read(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl", clock=lambda: datetime(2026, 3, 31, 23, 59, 59))
    log.write(make_record())
    data = json.loads(read_lines(tmp_path / "actions-2026-03.jsonl")[0])
    assert data["ts"].startswith("2026-03-31T23:59:59")


def test_cyrillic_is_stored_unescaped_and_round_trips(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl")
    log.write(make_record())
    raw = log.current_path().read_bytes().decode("utf-8")
    assert "открой калькулятор" in raw
    assert "\\u" not in raw
    data = json.loads(raw)
    assert data["text"] == "открой калькулятор"
    assert data["args"] == {"name": "Калькулятор"}


def test_log_is_append_only_across_instances(tmp_path):
    base = tmp_path / "actions.jsonl"
    ActionLog(base).write(make_record(request_id="a"))
    first = ActionLog(base).current_path().read_bytes()
    ActionLog(base).write(make_record(request_id="b"))
    content = ActionLog(base).current_path().read_bytes()
    assert content.startswith(first)
    assert [json.loads(x)["request_id"] for x in content.decode().splitlines()] == ["a", "b"]


def test_newlines_in_text_stay_on_one_line(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl")
    log.write(make_record(text="line1\nline2\r\nline3", summary="a\nb"))
    lines = read_lines(log.current_path())
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "line1\nline2\r\nline3"


def test_creates_missing_directory(tmp_path):
    log = ActionLog(tmp_path / "deep" / "dir" / "actions.jsonl")
    log.write(make_record())
    assert log.current_path().exists()


def test_monthly_rotation_switches_file(tmp_path):
    now = {"v": datetime(2026, 1, 31, 23, 59)}
    log = ActionLog(tmp_path / "actions.jsonl", clock=lambda: now["v"])
    log.write(make_record(request_id="jan"))
    now["v"] = datetime(2026, 2, 1, 0, 1)
    log.write(make_record(request_id="feb"))
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "actions-2026-01.jsonl",
        "actions-2026-02.jsonl",
    ]
    assert json.loads(read_lines(tmp_path / "actions-2026-01.jsonl")[0])["request_id"] == "jan"
    assert json.loads(read_lines(tmp_path / "actions-2026-02.jsonl")[0])["request_id"] == "feb"


def test_registered_secret_is_masked_in_args_and_other_fields(tmp_path):
    register_secret("my-registered-secret")
    log = ActionLog(tmp_path / "actions.jsonl")
    log.write(
        make_record(
            args={"url": "http://x/?key=my-registered-secret", "nested": ["my-registered-secret"]},
            text="use my-registered-secret",
            summary="failed: gsk_TESTTESTTESTTESTTEST",
        )
    )
    raw = log.current_path().read_text(encoding="utf-8")
    assert "my-registered-secret" not in raw
    assert "gsk_TEST" not in raw
    data = json.loads(raw)
    assert data["args"]["nested"] == [MASK]
    assert data["text"] == f"use {MASK}"


def test_non_json_args_do_not_break_writing(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl")
    log.write(make_record(args={"obj": object, "set": {1}}))
    assert len(read_lines(log.current_path())) == 1


def test_record_is_frozen():
    with pytest.raises(AttributeError):
        make_record().ok = False  # type: ignore[misc]


def test_concurrent_writes_produce_valid_lines(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl")
    barrier = threading.Barrier(50)

    def worker(i):
        barrier.wait()
        log.write(make_record(request_id=str(i), text="я" * 5000, summary=f"s{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = read_lines(log.current_path())
    assert len(lines) == 50
    assert {json.loads(x)["request_id"] for x in lines} == {str(i) for i in range(50)}


def test_write_returns_true_on_success(tmp_path):
    assert ActionLog(tmp_path / "actions.jsonl").write(make_record()) is True


def test_failed_write_returns_false_and_warns_without_payload(tmp_path, caplog):
    log = ActionLog(tmp_path / "actions.jsonl")
    log.current_path().mkdir()  # a directory where the file should be: open() fails
    with caplog.at_level(logging.WARNING, logger="luna.logs.action_log"):
        ok = log.write(make_record(request_id="req-42", text="private words"))
    assert ok is False
    warnings = [r for r in caplog.records if r.name == "luna.logs.action_log"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "open_app" in message
    assert "req-42" in message
    assert "private words" not in message


def test_directory_removed_after_init_is_recreated(tmp_path):
    base = tmp_path / "logs" / "actions.jsonl"
    log = ActionLog(base)
    shutil.rmtree(base.parent)
    assert log.write(make_record()) is True
    assert len(read_lines(log.current_path())) == 1
