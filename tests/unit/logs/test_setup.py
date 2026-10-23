import logging
from pathlib import Path

from luna.config.settings import Settings
from luna.logs.redact import MASK
from luna.logs.setup import BACKUP_COUNT, LOG_FILENAME, MAX_BYTES, default_log_dir, setup_logging

FAKE_GROQ = "gsk_TESTTESTTESTTESTTEST"


def flush_all():
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_default_log_dir_uses_localappdata():
    assert default_log_dir({"LOCALAPPDATA": "C:/X"}) == Path("C:/X") / "LUNA" / "logs"


def test_writes_to_file_in_given_dir_with_redaction(tmp_path):
    path = setup_logging("INFO", tmp_path / "logs", console=False)
    assert path == tmp_path / "logs" / LOG_FILENAME
    logging.getLogger("luna.test").info("token %s и кириллица", FAKE_GROQ)
    flush_all()
    text = path.read_text(encoding="utf-8")
    assert FAKE_GROQ not in text
    assert f"token {MASK} и кириллица" in text


def test_secrets_argument_masks_exact_values_including_exception(tmp_path):
    path = setup_logging("INFO", tmp_path, console=False, secrets=["plain-secret-value"])
    try:
        raise ValueError("bad key plain-secret-value")
    except ValueError:
        logging.getLogger("luna.test").exception("failed")
    flush_all()
    text = path.read_text(encoding="utf-8")
    assert "plain-secret-value" not in text
    assert "ValueError" in text


def test_settings_secret_values_reach_the_log(tmp_path):
    settings = Settings().with_api_keys({"GEMINI_API_KEY": "value-from-settings-1"})
    path = setup_logging("INFO", tmp_path, console=False, secrets=settings.secret_values())
    logging.getLogger("luna.test").info("using value-from-settings-1")
    flush_all()
    assert "value-from-settings-1" not in path.read_text(encoding="utf-8")


def test_console_handler_is_redacted_and_optional(tmp_path, capsys):
    setup_logging("INFO", tmp_path, console=True)
    logging.getLogger("luna.test").warning("key %s", FAKE_GROQ)
    err = capsys.readouterr().err
    assert FAKE_GROQ not in err
    assert MASK in err

    setup_logging("INFO", tmp_path, console=False)
    logging.getLogger("luna.test").info("silent")
    assert "silent" not in capsys.readouterr().err


def test_repeated_setup_does_not_duplicate_handlers(tmp_path):
    root = logging.getLogger()
    before = len(root.handlers)
    for _ in range(3):
        path = setup_logging("INFO", tmp_path, console=True)
    assert len(root.handlers) == before + 2
    logging.getLogger("luna.test").info("once")
    flush_all()
    assert path.read_text(encoding="utf-8").count("once") == 1


def test_rotation_limits_are_five_files_of_two_mb():
    assert MAX_BYTES == 2 * 1024 * 1024
    assert BACKUP_COUNT + 1 == 5


def test_rotation_happens_when_limit_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr("luna.logs.setup.MAX_BYTES", 2000)
    setup_logging("INFO", tmp_path, console=False)
    log = logging.getLogger("luna.test")
    for i in range(200):
        log.info("line %d %s", i, "x" * 50)
    flush_all()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["luna.log", "luna.log.1", "luna.log.2", "luna.log.3", "luna.log.4"]
    assert all(p.stat().st_size <= 2000 + 200 for p in tmp_path.iterdir())


def test_real_localappdata_is_not_touched_when_dir_given(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "must-stay-empty"))
    setup_logging("INFO", tmp_path / "logs", console=False)
    logging.getLogger("luna.test").info("x")
    assert not (tmp_path / "must-stay-empty").exists()


def test_console_is_quiet_but_file_keeps_info(tmp_path, capsys):
    path = setup_logging("INFO", tmp_path, console=True)
    logging.getLogger("luna.test").info("routine note")
    logging.getLogger("luna.test").warning("real warning")
    flush_all()
    err = capsys.readouterr().err
    assert "routine note" not in err
    assert "real warning" in err
    assert "routine note" in path.read_text(encoding="utf-8")


def test_third_party_info_is_dropped_everywhere(tmp_path, capsys):
    path = setup_logging("INFO", tmp_path, console=True)
    logging.getLogger("primp").info("response: https://search.example/?q=secret+query")
    logging.getLogger("httpx").info("HTTP Request: POST https://x")
    logging.getLogger("trafilatura.core").info("noise")
    logging.getLogger("luna.test").info("own record")  # creates the (lazily opened) file
    flush_all()
    assert "secret+query" not in capsys.readouterr().err
    assert "secret+query" not in path.read_text(encoding="utf-8")
    assert logging.getLogger("primp").getEffectiveLevel() == logging.WARNING
