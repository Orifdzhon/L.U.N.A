"""Speaker.active and activity listeners (half-duplex support, issue #109)."""

import itertools
import threading
import time

import pytest

from fakes import FakePlayer, FakeTTS
from luna.config.settings import TTSSettings
from luna.core.errors import AudioError
from luna.core.events import EventBus
from luna.core.models import Reply
from luna.tts.speaker import Speaker

TWO = "One sentence here. Another sentence here."
WAIT = 3.0


def wait_for(cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class ErrorPlayer(FakePlayer):
    def play(self, clip):
        self.played.append(clip)
        raise AudioError("no device")


@pytest.fixture
def bus():
    b = EventBus()
    yield b
    b.close()


@pytest.fixture
def make(bus):
    made = []

    def factory(player=None, tts=None):
        sp = Speaker(tts or FakeTTS(), player or FakePlayer(), bus, TTSSettings())
        made.append(sp)
        changes = []
        sp.add_activity_listener(changes.append)
        return sp, changes

    yield factory
    for sp in made:
        sp.close()


def reply(text=TWO):
    return Reply(text=text, lang="en")


def test_active_is_true_synchronously_and_false_after_play_returns(make):
    player = FakePlayer(block=True)
    sp, changes = make(player=player)
    assert not sp.active
    sp.speak(reply("Hello there."))
    assert sp.active and changes == [True]  # before any audio
    assert player.started.wait(WAIT)
    assert sp.active
    player.finish()
    assert wait_for(lambda: not sp.active)
    assert player.returned == 1
    assert changes == [True, False]


def test_queued_utterance_stays_active_without_flicker(make):
    player = FakePlayer(block=True)
    sp, changes = make(player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    player.finish()  # chunk 1 ends, chunk 2 follows
    assert wait_for(lambda: len(player.played) == 2)
    assert sp.active
    player.finish()
    assert wait_for(lambda: not sp.active)
    assert changes == [True, False]


def test_new_speak_supersedes_without_a_false_in_between(make):
    player = FakePlayer(block=True)
    sp, changes = make(player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.speak(reply("Second one."))  # barge-in
    assert wait_for(lambda: len(player.played) >= 2)
    player.finish()
    assert wait_for(lambda: not sp.active)
    assert changes == [True, False]


def test_stop_releases_only_after_play_returned(make):
    player = FakePlayer(block=True)
    sp, changes = make(player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.stop()
    assert wait_for(lambda: not sp.active)
    assert player.returned == 1
    assert changes == [True, False]
    sp.stop()  # idempotent: no duplicate transition
    time.sleep(0.05)
    assert changes == [True, False]


def test_stop_before_the_worker_took_the_job(make):
    tts = FakeTTS(delay=0.2)
    sp, changes = make(tts=tts)
    sp.speak(reply())
    sp.stop()
    assert wait_for(lambda: not sp.active)
    assert changes[0] is True and changes[-1] is False
    assert all(a != b for a, b in itertools.pairwise(changes))


def test_player_error_releases(make):
    sp, changes = make(player=ErrorPlayer())
    sp.speak(reply())
    assert wait_for(lambda: changes == [True, False])
    assert not sp.active


def test_synthesis_error_and_empty_text_release(make):
    sp, changes = make(tts=FakeTTS(error=RuntimeError("boom")))
    sp.speak(reply())
    assert wait_for(lambda: changes == [True, False])
    sp.speak(reply("   "))
    assert wait_for(lambda: changes == [True, False, True, False])


def test_muted_speaker_never_becomes_active(make):
    sp, changes = make()
    sp.set_muted(True)
    sp.speak(reply())
    time.sleep(0.1)
    assert not sp.active and changes == []


def test_set_muted_cuts_speech_and_releases(make):
    player = FakePlayer(block=True)
    sp, changes = make(player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.set_muted(True)
    assert wait_for(lambda: not sp.active)
    assert changes == [True, False]


def test_close_releases(make):
    player = FakePlayer(block=True)
    sp, changes = make(player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.close()
    assert not sp.active and changes == [True, False]


def test_raising_listener_does_not_break_speech_or_other_listeners(make):
    player = FakePlayer()
    sp, changes = make(player=player)

    def bad(active):
        raise RuntimeError("listener bug")

    sp.add_activity_listener(bad)
    later = []
    sp.add_activity_listener(later.append)
    sp.speak(reply())
    assert wait_for(lambda: later == [True, False])
    assert len(player.played) == 2 and changes == [True, False]


def test_unsubscribe_stops_notifications_and_is_idempotent(make):
    sp, _ = make()
    seen = []
    remove = sp.add_activity_listener(seen.append)
    sp.speak(reply("Hi."))
    assert wait_for(lambda: seen == [True, False])
    remove()
    remove()
    sp.speak(reply("Hi again."))
    assert wait_for(lambda: not sp.active and sp.active is False)
    time.sleep(0.1)
    assert seen == [True, False]


def test_listener_may_unsubscribe_itself(make):
    sp, _ = make()
    seen = []
    holder = {}

    def once(active):
        seen.append(active)
        holder["remove"]()

    holder["remove"] = sp.add_activity_listener(once)
    sp.speak(reply("Hi."))
    assert wait_for(lambda: not sp.active)
    assert seen == [True]


def test_concurrent_speaks_keep_transitions_alternating(make):
    sp, changes = make()

    def hammer():
        for _ in range(20):
            sp.speak(reply("Hi."))
            sp.stop()

    threads = [threading.Thread(target=hammer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wait_for(lambda: not sp.active)
    assert changes[0] is True and changes[-1] is False
    assert all(a != b for a, b in itertools.pairwise(changes))
