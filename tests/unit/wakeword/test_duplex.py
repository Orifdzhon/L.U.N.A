"""Half-duplex binding Speaker -> WakeListener (issue #109, ADR 0004 amendment)."""

import threading
import time

import pytest

from fakes import FakeClock, FakeLiveMic, FakePlayer, FakeTTS, FakeVAD, FakeWakeWord
from luna.config.settings import TTSSettings, VoiceSettings, WakeWordSettings
from luna.core.events import EventBus
from luna.core.models import Reply
from luna.tts.speaker import Speaker
from luna.wakeword.duplex import bind_speaker
from luna.wakeword.listener import WakeListener

WAIT = 3.0
FRAME_SAMPLES = 480


def wake_frame(i):
    return b"WAKE" + bytes([i % 250 + 1]) * 956


def wait_until(pred, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.002)
    return False


class Rig:
    def __init__(self, conf):
        self.conf = conf
        self.mic = FakeLiveMic()
        self.clock = FakeClock()
        self.bus = EventBus()
        self.triggers = []
        self.player = FakePlayer(block=True)
        self.listener = WakeListener(
            self.mic,
            FakeWakeWord(),
            lambda: FakeVAD(),
            conf,
            VoiceSettings(),
            self.bus,
            on_wake=lambda t: self.triggers.append(t) or True,
            clock=self.clock,
            read_timeout_s=0.01,
        )
        self.speaker = Speaker(FakeTTS(), self.player, self.bus, TTSSettings())
        self.fed = 0

    def feed(self, frame):
        self.mic.feed(frame)
        self.fed += 1
        assert wait_until(lambda: self.listener.position == self.fed * FRAME_SAMPLES)

    def close(self):
        self.speaker.close()
        self.listener.close()
        self.bus.close()


@pytest.fixture
def rig():
    made = []

    def factory(conf=None):
        r = Rig(conf or WakeWordSettings(cooldown_s=0.0, speaking_tail_ms=400))
        made.append(r)
        return r

    yield factory
    for r in made:
        r.close()


def say(r, text="Hello there."):
    r.speaker.speak(Reply(text=text, lang="en"))


def test_detection_is_suppressed_while_speaking_and_during_the_tail(rig):
    r = rig()
    unbind = bind_speaker(r.speaker, r.listener, r.conf)
    r.listener.start()
    say(r)
    assert r.player.started.wait(WAIT)
    r.feed(wake_frame(0))
    assert r.triggers == []
    r.player.finish()
    assert wait_until(lambda: not r.speaker.active)
    r.clock.advance(0.2)  # inside the 400 ms tail
    r.feed(wake_frame(1))
    assert r.triggers == []
    r.clock.advance(0.3)  # tail over
    r.feed(b"\x00" * 960)
    r.feed(wake_frame(2))
    assert wait_until(lambda: len(r.triggers) == 1)
    unbind()


def test_mute_while_speaking_false_binds_nothing(rig):
    r = rig(WakeWordSettings(cooldown_s=0.0, mute_while_speaking=False))
    unbind = bind_speaker(r.speaker, r.listener, r.conf)
    r.listener.start()
    say(r)
    assert r.player.started.wait(WAIT)
    r.feed(wake_frame(0))
    assert wait_until(lambda: len(r.triggers) == 1)
    unbind()


def test_unbind_releases_the_hold_and_stops_following(rig):
    r = rig(WakeWordSettings(cooldown_s=0.0, speaking_tail_ms=0))
    unbind = bind_speaker(r.speaker, r.listener, r.conf)
    r.listener.start()
    say(r)
    assert r.player.started.wait(WAIT)
    assert wait_until(lambda: "speaking" in r.listener._gate.holds)
    unbind()
    unbind()
    assert "speaking" not in r.listener._gate.holds
    r.speaker.stop()
    say(r, "Again.")
    time.sleep(0.1)
    assert "speaking" not in r.listener._gate.holds


def test_bind_in_the_middle_of_an_utterance_picks_up_the_hold(rig):
    r = rig()
    say(r)
    assert r.player.started.wait(WAIT)
    unbind = bind_speaker(r.speaker, r.listener, r.conf)
    assert "speaking" in r.listener._gate.holds
    unbind()


def test_listener_set_hold_from_speaker_callback_cannot_deadlock(rig):
    r = rig()
    r.listener.start()
    bind_speaker(r.speaker, r.listener, r.conf)
    # Hammer both locks from several threads: FrontEnd -> Speaker -> Listener only.
    stop = threading.Event()

    def feeder():
        i = 0
        while not stop.is_set():
            r.mic.feed(wake_frame(i))
            i += 1
            time.sleep(0.001)

    def talker():
        for _ in range(30):
            say(r, "Hi.")
            r.speaker.stop()
            r.player.finish()

    threads = [threading.Thread(target=feeder, daemon=True)]
    threads += [threading.Thread(target=talker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads[1:]:
        t.join(10)
        assert not t.is_alive(), "deadlock between Speaker and WakeListener"
    stop.set()
    assert wait_until(lambda: not r.speaker.active)
    assert wait_until(lambda: "speaking" not in r.listener._gate.holds)
