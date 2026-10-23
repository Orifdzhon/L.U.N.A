"""Plays a real tone (manual: ``uv sync --extra voice`` then ``uv run pytest -m audio``)."""

import math
import struct
import threading
import time

import pytest

from luna.audio.base import AudioClip
from luna.audio.playback import SoundDevicePlayer

pytestmark = pytest.mark.audio


def tone(seconds: float, hz: float = 440.0, rate: int = 22050) -> AudioClip:
    n = int(seconds * rate)
    samples = (int(6000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))
    return AudioClip(b"".join(struct.pack("<h", s) for s in samples), rate)


def test_tone_plays_and_is_cut_off_by_stop():
    sd = pytest.importorskip("sounddevice")
    try:
        if sd.query_devices(kind="output") is None:
            pytest.skip("no audio output device")
    except Exception as e:
        pytest.skip(f"no audio output device: {e}")
    player = SoundDevicePlayer()
    try:
        player.play(tone(0.5))  # you should hear a short beep
        long = tone(5.0)
        threading.Timer(1.0, player.stop).start()
        start = time.monotonic()
        player.play(long)  # cut off after about 1 s
        assert time.monotonic() - start < 2.0
    finally:
        player.close()
