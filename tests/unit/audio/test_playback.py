import threading
import time

import pytest

from fakes import FakePlayer
from luna.audio.base import AudioClip, AudioPlayer
from luna.audio.playback import SoundDevicePlayer
from luna.core.errors import AudioError


class FakeStream:
    """Simulates a real-time device: each write() 'plays' for write_delay_s unless aborted."""

    def __init__(self, write_delay_s: float = 0.02, raise_after_abort: bool = False) -> None:
        self.delay = write_delay_s
        self.raise_after_abort = raise_after_abort
        self.writing = threading.Event()  # set on the first write()
        self.written = 0
        self.events: list[str] = []
        self._aborted = threading.Event()

    def start(self) -> None:
        self.events.append("start")

    def write(self, data: bytes) -> None:
        if self.raise_after_abort and self._aborted.is_set():
            raise OSError("stream aborted")
        self.written += len(data)
        self.writing.set()
        self._aborted.wait(self.delay)
        if self.raise_after_abort and self._aborted.is_set():
            raise OSError("stream aborted")

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
    assert stream.writing.wait(1.0)
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
    assert stream.writing.wait(1.0)
    player.stop()
    t.join(1.0)
    assert not t.is_alive()


def test_write_error_caused_by_stop_is_swallowed():
    stream = FakeStream(write_delay_s=0.05, raise_after_abort=True)
    player = make_player(stream)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            player.play(clip(30.0))
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    assert stream.writing.wait(1.0)
    player.stop()
    t.join(1.0)
    assert not t.is_alive() and errors == []
    assert stream.events[-1] == "close"


def test_write_error_without_stop_becomes_audio_error():
    class Broken(FakeStream):
        def write(self, data: bytes) -> None:
            raise OSError("device lost")

    stream = Broken()
    with pytest.raises(AudioError, match="device lost"):
        make_player(stream).play(clip(0.1))
    assert stream.events[-1] == "close"


def test_close_error_does_not_mask_original_error():
    class Broken(FakeStream):
        def write(self, data: bytes) -> None:
            raise OSError("write failed")

        def close(self) -> None:
            raise RuntimeError("close failed")

    with pytest.raises(AudioError, match="write failed"):
        make_player(Broken()).play(clip(0.1))


def test_open_failure_becomes_audio_error():
    def factory(rate: int) -> FakeStream:
        raise OSError("no device")

    with pytest.raises(AudioError, match="no device"):
        SoundDevicePlayer(factory).play(clip(0.1))


def test_missing_backend_gives_install_hint():
    def factory(rate: int) -> FakeStream:
        raise ImportError("No module named sounddevice")

    with pytest.raises(AudioError, match=r"uv sync"):
        SoundDevicePlayer(factory).play(clip(0.1))


def test_stop_during_device_open_is_not_lost():
    stream = FakeStream(write_delay_s=0)
    opening, release = threading.Event(), threading.Event()

    def factory(rate: int) -> FakeStream:
        opening.set()
        assert release.wait(2.0)
        return stream

    player = SoundDevicePlayer(factory)
    t = threading.Thread(target=player.play, args=(clip(1.0),))
    t.start()
    assert opening.wait(1.0)
    player.stop()
    release.set()
    t.join(1.0)
    assert not t.is_alive()
    assert stream.written == 0
    assert stream.events == ["abort", "close"]


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
