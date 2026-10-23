import threading
import time

import pytest

from fakes import FakePlayer, FakeTTS
from luna.audio.base import AudioClip
from luna.config.settings import TTSSettings
from luna.core.errors import AudioError, ProviderUnavailable
from luna.core.events import CLOSED, EventBus, Status, StatusChanged
from luna.core.models import Reply
from luna.tts.speaker import Speaker

TEXT = "Первое предложение. Второе предложение. Третье предложение."
FIRST = "Первое предложение."
WAIT = 3.0


def wait_for(cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class GatedTTS(FakeTTS):
    """Synthesis of the gated texts blocks until ``release(text)``."""

    def __init__(self, gated=()):
        super().__init__()
        self._gates = {t: threading.Event() for t in gated}
        self.entered = {t: threading.Event() for t in gated}

    def release(self, text):
        self._gates[text].set()

    def synthesize(self, text, lang):
        if text in self._gates:
            self.entered[text].set()
            self._gates[text].wait(WAIT)
        return super().synthesize(text, lang)


class ScriptedPlayer(FakePlayer):
    """Blocking player; optionally raises ``AudioError`` on the n-th clip."""

    def __init__(self, error_on=None):
        super().__init__(block=True)
        self.error_on = error_on

    def play(self, clip):
        if self.error_on is not None and len(self.played) + 1 == self.error_on:
            self.played.append(clip)
            raise AudioError("no device")
        super().play(clip)


@pytest.fixture
def bus():
    b = EventBus()
    yield b
    b.close()


def make(bus, tts=None, player=None):
    tts = tts or FakeTTS()
    player = player or FakePlayer()
    return Speaker(tts, player, bus, TTSSettings()), tts, player


def statuses(sub):
    out = []
    while not sub.queue.empty():
        item = sub.queue.get_nowait()
        if item is not CLOSED:
            out.append(item)
    return out


def wait_status(sub, status, timeout=WAIT):
    got = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got += [e for e in statuses(sub) if isinstance(e, StatusChanged)]
        if any(e.status == status for e in got):
            return got
        time.sleep(0.005)
    raise AssertionError(f"no {status}: {got}")


def reply(text=TEXT, lang="ru"):
    return Reply(text=text, lang=lang)


def worker_threads():
    return [t for t in threading.enumerate() if t.name.startswith("luna-speaker")]


def test_chunks_synthesized_and_played_in_order_with_lang(bus):
    sp, tts, player = make(bus)
    sub = bus.subscribe()
    sp.speak(reply(lang="ru"), "r1")
    got = wait_status(sub, Status.IDLE)
    assert [c.text for c in tts.calls] == [FIRST, "Второе предложение.", "Третье предложение."]
    assert {c.lang for c in tts.calls} == {"ru"}
    assert len(player.played) == 3
    assert [(e.status, e.request_id) for e in got] == [
        (Status.SPEAKING, "r1"),
        (Status.IDLE, "r1"),
    ]
    sp.close()


def test_lang_falls_back_to_detection(bus):
    sp, tts, _ = make(bus)
    sub = bus.subscribe()
    sp.speak(reply("Hello there. How are you?", lang=""))
    wait_status(sub, Status.IDLE)
    assert {c.lang for c in tts.calls} == {"en"}
    sp.close()


def test_look_ahead_synthesizes_next_chunk_while_current_plays(bus):
    player = ScriptedPlayer()
    sp, tts, _ = make(bus, player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    # Chunk 1 is playing (blocked): chunk 2 gets synthesized, chunk 3 must wait.
    assert wait_for(lambda: len(tts.calls) == 2)
    time.sleep(0.15)
    assert len(tts.calls) == 2
    assert len(player.played) == 1
    player.finish()
    assert wait_for(lambda: len(tts.calls) == 3)
    sp.close()


def test_speak_returns_immediately_even_when_everything_blocks(bus):
    tts = GatedTTS(gated=[FIRST])
    sp, _, _ = make(bus, tts=tts, player=ScriptedPlayer())
    t0 = time.monotonic()
    sp.speak(reply())
    assert time.monotonic() - t0 < 0.05
    assert tts.entered[FIRST].wait(WAIT)
    t0 = time.monotonic()
    sp.speak(reply("Другое. Ответ."))  # while synthesis blocks
    assert time.monotonic() - t0 < 0.05
    tts.release(FIRST)
    sp.close()


def test_speak_returns_immediately_while_player_blocks(bus):
    player = ScriptedPlayer()
    sp, _, _ = make(bus, player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    t0 = time.monotonic()
    sp.speak(reply("Другое."))
    assert time.monotonic() - t0 < 0.05
    sp.close()


def test_stop_mid_sentence_gives_idle_and_no_more_plays(bus):
    player = ScriptedPlayer()
    sp, _, _ = make(bus, player=player)
    sub = bus.subscribe()
    sp.speak(reply(), "r1")
    assert player.started.wait(WAIT)
    t0 = time.monotonic()
    sp.stop()
    got = wait_status(sub, Status.IDLE, 0.5)
    assert time.monotonic() - t0 < 0.5
    time.sleep(0.15)
    assert len(player.played) == 1
    assert [e.status for e in got] == [Status.SPEAKING, Status.IDLE]
    sp.stop()  # idempotent
    assert statuses(sub) == []
    sp.close()


def test_stop_when_idle_publishes_nothing(bus):
    sp, _, _ = make(bus)
    sub = bus.subscribe()
    sp.stop()
    sp.stop()
    assert statuses(sub) == []
    sp.close()


def test_stop_during_synthesis_discards_result(bus):
    tts = GatedTTS(gated=[FIRST])
    sp, _, player = make(bus, tts=tts)
    sub = bus.subscribe()
    sp.speak(reply(), "r1")
    assert tts.entered[FIRST].wait(WAIT)
    sp.stop()
    tts.release(FIRST)
    time.sleep(0.2)
    assert player.played == []
    assert statuses(sub) == []  # never spoke: no SPEAKING, no IDLE
    sp.close()


def test_new_speak_supersedes_old(bus):
    player = ScriptedPlayer()
    sp, tts, _ = make(bus, player=player)
    sub = bus.subscribe()
    sp.speak(reply(), "old")
    assert player.started.wait(WAIT)
    player.started.clear()
    sp.speak(reply("Новый ответ. Ещё один.", "ru"), "new")
    assert player.started.wait(WAIT)
    player.finish()
    assert wait_for(lambda: len(player.played) == 3)
    player.finish()
    got = wait_status(sub, Status.IDLE)
    got += wait_status(sub, Status.IDLE)
    assert [(e.status, e.request_id) for e in got][:3] == [
        (Status.SPEAKING, "old"),
        (Status.IDLE, "old"),
        (Status.SPEAKING, "new"),
    ]
    spoken = [c.text for c in tts.calls]
    assert "Третье предложение." not in spoken
    sp.close()


def test_rapid_speaks_play_only_the_last_utterance_fully(bus):
    sp, tts, _ = make(bus)
    sub = bus.subscribe()
    for i in range(20):
        sp.speak(reply("Раз. Два."), f"r{i}")
    got = []
    end = time.monotonic() + WAIT
    while time.monotonic() < end and not any(
        e.request_id == "r19" and e.status == Status.IDLE for e in got
    ):
        got += [e for e in statuses(sub) if isinstance(e, StatusChanged)]
        time.sleep(0.01)
    assert any(e.request_id == "r19" and e.status == Status.IDLE for e in got)
    assert tts.calls[-1].text == "Два."
    sp.close()


def test_muted_plays_nothing_and_publishes_nothing(bus):
    sp, tts, player = make(bus)
    sub = bus.subscribe()
    sp.set_muted(True)
    assert sp.muted
    sp.speak(reply(), "r1")
    time.sleep(0.15)
    assert tts.calls == [] and player.played == []
    assert statuses(sub) == []
    sp.set_muted(False)
    sp.speak(reply("Привет."), "r2")
    wait_status(sub, Status.IDLE)
    assert len(player.played) == 1
    sp.close()


def test_muting_stops_current_speech(bus):
    player = ScriptedPlayer()
    sp, _, _ = make(bus, player=player)
    sub = bus.subscribe()
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.set_muted(True)
    wait_status(sub, Status.IDLE, 0.5)
    time.sleep(0.1)
    assert len(player.played) == 1
    sp.close()


def test_provider_error_publishes_error_then_idle_and_recovers(bus):
    tts = FakeTTS(error=ProviderUnavailable("boom"))
    sp, _, player = make(bus, tts=tts)
    sub = bus.subscribe()
    sp.speak(reply(), "r1")
    got = wait_status(sub, Status.IDLE)
    assert [(e.status, e.request_id, e.detail) for e in got] == [
        (Status.ERROR, "r1", "tts_failed"),
        (Status.IDLE, "r1", None),
    ]
    assert player.played == []
    tts.error = None
    sp.speak(reply("Привет."), "r2")
    got = wait_status(sub, Status.IDLE)
    assert [e.status for e in got] == [Status.SPEAKING, Status.IDLE]
    sp.close()


def test_player_error_publishes_error_once_then_idle(bus):
    player = ScriptedPlayer(error_on=2)
    sp, _, _ = make(bus, player=player)
    sub = bus.subscribe()
    sp.speak(reply(), "r1")
    assert player.started.wait(WAIT)
    player.finish()
    got = wait_status(sub, Status.IDLE)
    assert [e.status for e in got] == [Status.SPEAKING, Status.ERROR, Status.IDLE]
    assert got[1].detail == "tts_failed"
    assert len(player.played) == 2  # nothing after the failed clip
    sp.close()


def test_error_on_later_chunk_after_speaking(bus):
    class FlakyTTS(FakeTTS):
        def synthesize(self, text, lang):
            if text.startswith("Второе"):
                raise ProviderUnavailable("late")
            return super().synthesize(text, lang)

    sp, _, player = make(bus, tts=FlakyTTS())
    sub = bus.subscribe()
    sp.speak(reply())
    got = wait_status(sub, Status.IDLE)
    assert [e.status for e in got] == [Status.SPEAKING, Status.ERROR, Status.IDLE]
    assert len(player.played) == 1
    sp.close()


def test_empty_text_is_silent(bus):
    sp, tts, _ = make(bus)
    sub = bus.subscribe()
    sp.speak(reply("   "))
    time.sleep(0.1)
    assert tts.calls == [] and statuses(sub) == []
    sp.close()


def test_close_leaves_no_threads_and_closes_player(bus):
    before = len(worker_threads())
    player = ScriptedPlayer()
    sp, _, _ = make(bus, player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.close()
    assert wait_for(lambda: len(worker_threads()) == before)
    assert player.closed
    sp.close()  # idempotent
    sp.speak(reply())  # no-op after close
    assert len(player.played) == 1


def test_close_while_synthesis_blocks(bus):
    tts = GatedTTS(gated=[FIRST])
    sp, _, _ = make(bus, tts=tts)
    sp.speak(reply())
    assert tts.entered[FIRST].wait(WAIT)
    tts.release(FIRST)
    sp.close()
    assert wait_for(lambda: worker_threads() == [])


def test_played_clips_are_audio_clips(bus):
    sp, _, player = make(bus)
    sub = bus.subscribe()
    sp.speak(reply("Привет."))
    wait_status(sub, Status.IDLE)
    assert isinstance(player.played[0], AudioClip)
    sp.close()
