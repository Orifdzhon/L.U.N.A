import io
import logging

import pytest

from luna.logs.redact import (
    MASK,
    RedactionFilter,
    redact_text,
    redact_value,
    register_secret,
)

FAKE_GROQ = "gsk_TESTTESTTESTTESTTEST"


@pytest.mark.parametrize(
    "key",
    [
        FAKE_GROQ,
        "AIzaSyTESTTESTTESTTESTTESTTEST12",
        "sk-TESTTESTTESTTEST",
        "sk-proj-AbC_123-xyzXYZ",
    ],
)
def test_key_shaped_tokens_are_masked(key):
    assert redact_text(f"calling with {key} now") == f"calling with {MASK} now"


def test_bearer_token_is_masked_but_scheme_kept():
    assert (
        redact_text("Authorization: Bearer abcDEF123456789.xyz-_~+/==")
        == f"Authorization: Bearer {MASK}"
    )


def test_ordinary_text_is_untouched():
    text = "Открой калькулятор, task-sk-1 and a skill-based risk"
    assert redact_text(text) == text


def test_registered_secret_is_masked_everywhere():
    register_secret("hunter2hunter2")
    assert redact_text("a hunter2hunter2 b hunter2hunter2") == f"a {MASK} b {MASK}"


def test_short_and_blank_secrets_are_ignored():
    for value in ("", "   ", "ab", None):
        register_secret(value)
    assert redact_text("ab ab") == "ab ab"


def test_longer_secret_wins_over_its_prefix():
    register_secret("secret1234")
    register_secret("secret1234-long-tail")
    assert redact_text("x secret1234-long-tail y") == f"x {MASK} y"


def test_redact_value_walks_nested_structures():
    register_secret("topsecretvalue")
    value = {
        "url": "http://x/?k=topsecretvalue",
        "n": 3,
        "ok": True,
        "none": None,
        "list": ["topsecretvalue", {"deep": ("topsecretvalue",)}],
    }
    out = redact_value(value)
    assert "topsecretvalue" not in repr(out)
    assert out["n"] == 3
    assert out["ok"] is True
    assert out["none"] is None
    assert out["list"][1]["deep"] == [MASK]


def test_redact_value_stringifies_unknown_objects_and_masks_them():
    register_secret("topsecretvalue")

    class Weird:
        def __str__(self):
            return "weird topsecretvalue"

    assert redact_value(Weird()) == f"weird {MASK}"


@pytest.fixture
def logger_and_stream():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("luna.test.redaction")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield logger, stream
    logger.removeHandler(handler)


def test_filter_masks_message_and_args(logger_and_stream):
    logger, stream = logger_and_stream
    register_secret("registered-secret-value")
    logger.info("key %s and %s", FAKE_GROQ, "registered-secret-value")
    assert stream.getvalue().strip() == f"INFO key {MASK} and {MASK}"


def test_filter_masks_secret_in_exception_traceback(logger_and_stream):
    logger, stream = logger_and_stream
    register_secret("registered-secret-value")
    try:
        raise RuntimeError(f"request failed for {FAKE_GROQ} / registered-secret-value")
    except RuntimeError:
        logger.exception("boom")
    out = stream.getvalue()
    assert "Traceback" in out
    assert "RuntimeError" in out
    assert FAKE_GROQ not in out
    assert "registered-secret-value" not in out


def test_filter_masks_chained_exceptions(logger_and_stream):
    logger, stream = logger_and_stream
    register_secret("registered-secret-value")
    try:
        try:
            raise KeyError("registered-secret-value")
        except KeyError as exc:
            raise RuntimeError("wrapper") from exc
    except RuntimeError:
        logger.exception("boom")
    out = stream.getvalue()
    assert "KeyError" in out
    assert "registered-secret-value" not in out


def test_filter_masks_stack_info(logger_and_stream):
    logger, stream = logger_and_stream
    register_secret("registered-secret-value")
    logger.warning("with stack registered-secret-value", stack_info=True)
    out = stream.getvalue()
    assert "Stack (most recent call last)" in out
    assert "registered-secret-value" not in out


def test_filter_survives_bad_format_args(logger_and_stream):
    logger, stream = logger_and_stream
    logger.info("only one placeholder %s", "a", "b")
    assert "only one placeholder" in stream.getvalue()


@pytest.mark.parametrize(
    "text",
    [
        "task-management-panel",
        "disk-usage-statistics",
        "risk-assessment-report",
        "my_gsk_something_long_name",
        "xAIzaSyTESTTESTTESTTESTTESTTEST12",
        "bearer authentication failed",
        "Bearer authentication failed",
        "Bearer token missing",
    ],
)
def test_ordinary_words_are_not_mangled(text):
    assert redact_text(text) == text


def test_key_after_punctuation_is_still_masked():
    assert redact_text("key=sk-TESTTESTTESTTEST,") == f"key={MASK},"
    assert redact_text("(gsk_TESTTESTTESTTESTTEST)") == f"({MASK})"
