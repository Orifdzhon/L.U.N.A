import threading
import time

import pytest

from fakes import FakePlayer
from luna.audio.base import AudioClip, AudioPlayer
from luna.audio.playback import SoundDevicePlayer


class FakeStream:
    """Simulates a real-time device: each write() 'plays' for write_delay_s unless aborted."""

    def __init__(self, write_delay_s: float = 0.02) -> None:
        self.delay = write_delay_s
        self.written = 0
        self.events: list[str] = []
        self._aborted = threading.Event()

    def start(self) -> None:
        self.events.append("start")

    def write(self, data: bytes) -> None:
        self.written += len(data)
        self._aborted.wait(self.delay)

    def stop(self) -> None:
        self.events.append("stop")

    def abort(self) -> None:
        self.events.append("abort")
        self._aborted.set()

    def close(self) -> None:
        self.events.append("close")


def clip(seconds: float, rate: int = 16000) -> AudioClip:
    return AudioClip(b"\x00\x00" * int(seconds * rate), rate)


def make_player(stream: FakeStream) -> SoundDevicePlayer:
    return SoundDevicePlayer(lambda rate: stream)


def test_clip_duration():
    assert AudioClip(b"\x00\x00" * 8000, 16000).duration_s == pytest.approx(0.5)
    assert AudioClip(b"", 16000).duration_s == 0.0


def test_clip_validation():
    with pytest.raises(ValueError):
        AudioClip(b"\x00\x00\x00", 16000)
    with pytest.raises(ValueError):
        AudioClip(b"\x00\x00", 0)
    with pytest.raises(ValueError):
        AudioClip(b"\x00\x00", -1)


def test_play_writes_everything_and_drains():
    stream = FakeStream(write_delay_s=0)
    c = clip(0.3)
    make_player(stream).play(c)
    assert stream.written == len(c.pcm)
    assert stream.events == ["start", "stop", "close"]


def test_empty_clip_is_noop():
    stream = FakeStream()
    make_player(stream).play(AudioClip(b"", 16000))
    assert stream.events == []


def test_play_blocks_until_done():
    stream = FakeStream(write_delay_s=0.02)
    start = time.monotonic()
    make_player(stream).play(clip(0.2))  # 4 chunks x 20 ms
    assert time.monotonic() - start >= 0.07


def test_stop_from_another_thread_returns_play_quickly():
    stream = FakeStream(write_delay_s=0.05)
    player = make_player(stream)
    done = threading.Event()
    t = threading.Thread(target=lambda: (player.play(clip(30.0)), done.set()))
    t.start()
    time.sleep(0.15)
    t0 = time.monotonic()
    player.stop()
    assert done.wait(0.5)
    assert time.monotonic() - t0 < 0.5
    t.join()
    assert "abort" in stream.events and "stop" not in stream.events
    assert stream.events[-1] == "close"
    assert stream.written < len(clip(30.0).pcm)


def test_stop_when_idle_and_double_stop_do_not_raise():
    player = make_player(FakeStream())
    player.stop()
    player.stop()
    player.close()
    player.close()


def test_stop_does_not_affect_next_play():
    first, second = FakeStream(0), FakeStream(0)
    streams = iter([first, second])
    player = SoundDevicePlayer(lambda rate: next(streams))
    player.stop()
    c = clip(0.1)
    player.play(c)
    player.stop()
    player.play(c)
    assert second.written == len(c.pcm) and "abort" not in second.events


def test_play_after_close_does_nothing():
    stream = FakeStream()
    player = make_player(stream)
    player.close()
    player.play(clip(0.1))
    assert stream.written == 0


def test_stop_survives_abort_error():
    class BadStream(FakeStream):
        def abort(self) -> None:
            super().abort()
            raise OSError("device gone")

    stream = BadStream(write_delay_s=0.05)
    player = make_player(stream)
    t = threading.Thread(target=player.play, args=(clip(30.0),))
    t.start()
    time.sleep(0.1)
    player.stop()
    t.join(1.0)
    assert not t.is_alive()


def test_players_satisfy_protocol():
    players: list[AudioPlayer] = [SoundDevicePlayer(), FakePlayer()]
    assert len(players) == 2


def test_fake_player_records_and_blocks_until_stopped():
    fake = FakePlayer(block=True)
    c = clip(0.1)
    t = threading.Thread(target=fake.play, args=(c,))
    t.start()
    assert fake.started.wait(1.0)
    assert t.is_alive()
    fake.stop()
    t.join(1.0)
    assert not t.is_alive()
    assert fake.played == [c] and fake.stop_count == 1
    fake.close()
    assert fake.closed
