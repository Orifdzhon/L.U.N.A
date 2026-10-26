from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from pathlib import Path

import pytest

from luna.logs import windowless


@pytest.fixture(autouse=True)
def _restore(monkeypatch: pytest.MonkeyPatch):
    saved = (sys.stdout, sys.stderr, sys.excepthook, threading.excepthook)
    was_enabled = faulthandler.is_enabled()
    yield
    for stream in (sys.stdout, sys.stderr):
        if stream not in saved[:2] and stream is not None:
            stream.close()
    sys.stdout, sys.stderr, sys.excepthook, threading.excepthook = saved
    faulthandler.disable()
    if was_enabled:
        faulthandler.enable()
    if windowless._crash_file is not None:
        windowless._crash_file.close()
    windowless._crash_file = None
    windowless._installed = False


def test_missing_streams_are_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    windowless.install(tmp_path)
    sys.stdout.write("x")
    sys.stderr.write("x")


def test_real_streams_untouched(tmp_path: Path) -> None:
    out, err = sys.stdout, sys.stderr
    windowless.install(tmp_path)
    assert sys.stdout is out
    assert sys.stderr is err


def test_excepthooks_log_critical(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    windowless.install(tmp_path)
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("boom")
        except ValueError as e:
            sys.excepthook(type(e), e, e.__traceback__)
            threading.excepthook(
                threading.ExceptHookArgs((type(e), e, e.__traceback__, threading.current_thread()))
            )
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 2
    assert all(r.exc_info for r in caplog.records)


def test_keyboard_interrupt_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    windowless.install(tmp_path)
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: None)
    with caplog.at_level(logging.ERROR):
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert not caplog.records


def test_faulthandler_and_idempotent(tmp_path: Path) -> None:
    windowless.install(tmp_path)
    assert faulthandler.is_enabled()
    assert (tmp_path / "crash.log").exists()
    first_file, hook = windowless._crash_file, sys.excepthook
    windowless.install(tmp_path / "other")
    assert windowless._crash_file is first_file
    assert sys.excepthook is hook
    assert not (tmp_path / "other").exists()
