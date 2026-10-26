"""FrameTap: backlog, overflow, close / error semantics (#107)."""

import threading
import time

import pytest

from luna.audio.capture import FrameTap
from luna.core.errors import AudioError


def f(i: int) -> bytes:
    return bytes([i]) * 4


def test_reads_backlog_then_live_frames_in_order():
    tap = FrameTap([f(1), f(2)])
    tap.push(f(3))
    assert [tap.read(0), tap.read(0), tap.read(0)] == [f(1), f(2), f(3)]
    assert tap.read(0.01) is None  # timeout, still open
    assert not tap.closed


def test_drops_oldest_when_full_and_counts():
    tap = FrameTap([f(1), f(2), f(3)], max_frames=3)
    tap.push(f(4))
    assert tap.dropped == 1
    assert [tap.read(0) for _ in range(3)] == [f(2), f(3), f(4)]


def test_oversized_backlog_keeps_the_latest_frames():
    tap = FrameTap([f(i) for i in range(10)], max_frames=4)
    assert tap.dropped == 6
    assert tap.read(0) == f(6)


def test_rejects_bad_max_frames():
    with pytest.raises(ValueError):
        FrameTap(max_frames=0)


def test_close_wakes_a_blocked_read():
    tap = FrameTap()
    out: list[object] = []
    t = threading.Thread(target=lambda: out.append(tap.read(5.0)))
    t.start()
    time.sleep(0.05)
    tap.close()
    t.join(2)
    assert not t.is_alive() and out == [None]
    assert tap.closed


def test_push_wakes_a_blocked_read():
    tap = FrameTap()
    out: list[object] = []
    t = threading.Thread(target=lambda: out.append(tap.read(5.0)))
    t.start()
    time.sleep(0.05)
    tap.push(f(7))
    t.join(2)
    assert out == [f(7)]


def test_close_discards_audio_and_ignores_push():
    tap = FrameTap([f(1)])
    tap.close()
    tap.push(f(2))
    assert tap.read(0) is None


def test_close_with_error_raises_once():
    tap = FrameTap()
    tap.close(AudioError("lost"))
    with pytest.raises(AudioError, match="lost"):
        tap.read(0)
    assert tap.read(0) is None


def test_close_is_idempotent_and_first_close_wins():
    tap = FrameTap()
    tap.close()
    tap.close(AudioError("late"))
    assert tap.read(0) is None
