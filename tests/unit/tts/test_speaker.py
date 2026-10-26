import threading
import time

import pytest

from fakes import FakePlayer, FakeTTS
from luna.audio.base import AudioClip
from luna.config.settings import TTSSettings
from luna.core.errors import AudioError, NothingToSpeak, ProviderUnavailable
from luna.core.events import CLOSED, EventBus, Status, StatusChanged
from luna.core.models import Reply
from luna.tts.speaker import Speaker

TEXT = "Первое предложение. Второе предложение. Третье предложение."
FIRST = "Первое предложение."
WAIT = 3.0
FAST = 0.2  # a call that "returns immediately"


def wait_for(cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class Events:
    """Accumulates the StatusChanged events of one subscription (never loses any)."""

    def __init__(self, bus):
        self.sub = bus.subscribe()
        self.seen: list[StatusChanged] = []

    def _drain(self):
        while not self.sub.queue.empty():
            item = self.sub.queue.get_nowait()
            if item is not CLOSED and isinstance(item, StatusChanged):
                self.seen.append(item)

    def all(self):
        self._drain()
        return list(self.seen)

    def wait(self, status, request_id=None, timeout=WAIT):
        def hit():
            return any(
                e.status == status and (request_id is None or e.request_id == request_id)
                for e in self.all()
            )

        assert wait_for(hit, timeout), f"no {status} {request_id}: {self.all()}"
        return self.all()

    def pairs(self):
        return [(e.status, e.request_id) for e in self.all()]


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


class ErrorOnNthPlayer(FakePlayer):
    """Blocking player that raises ``AudioError`` on the n-th clip."""

    def __init__(self, error_on):
        super().__init__(block=True)
        self.error_on = error_on

    def play(self, clip):
        if len(self.played) + 1 == self.error_on:
            self.played.append(clip)
            raise AudioError("no device")
        super().play(clip)


class StubbornPlayer(FakePlayer):
    """play() ignores stop() and always takes 0.3 s."""

    def play(self, clip):
        self.played.append(clip)
        self.started.set()
        time.sleep(0.3)
        self.returned += 1

    def stop(self):
        self.stop_count += 1


@pytest.fixture
def bus():
    b = EventBus()
    yield b
    b.close()


def make(bus, tts=None, player=None):
    tts = tts or FakeTTS()
    player = player or FakePlayer()
    return Speaker(tts, player, bus, TTSSettings()), tts, player


def reply(text=TEXT, lang="ru"):
    return Reply(text=text, lang=lang)


def worker_threads():
    return [t for t in threading.enumerate() if t.name.startswith("luna-speaker")]


def timed(fn, *args):
    t0 = time.monotonic()
    fn(*args)
    return time.monotonic() - t0


def test_chunks_synthesized_and_played_in_order_with_lang(bus):
    sp, tts, player = make(bus)
    ev = Events(bus)
    sp.speak(reply(lang="ru"), "r1")
    ev.wait(Status.IDLE)
    assert [c.text for c in tts.calls] == [FIRST, "Второе предложение.", "Третье предложение."]
    assert {c.lang for c in tts.calls} == {"ru"}
    assert len(player.played) == 3
    assert ev.pairs() == [(Status.SPEAKING, "r1"), (Status.IDLE, "r1")]
    sp.close()


def test_lang_falls_back_to_detection(bus):
    sp, tts, _ = make(bus)
    ev = Events(bus)
    sp.speak(reply("Hello there. How are you?", lang=""))
    ev.wait(Status.IDLE)
    assert {c.lang for c in tts.calls} == {"en"}
    sp.close()


def test_look_ahead_synthesizes_next_chunk_while_current_plays(bus):
    player = FakePlayer(block=True)
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
    sp, _, _ = make(bus, tts=tts, player=FakePlayer(block=True))
    assert timed(sp.speak, reply()) < FAST
    assert tts.entered[FIRST].wait(WAIT)
    assert timed(sp.speak, reply("Другое. Ответ.")) < FAST  # while synthesis blocks
    tts.release(FIRST)
    sp.close()


def test_speak_and_stop_do_not_wait_for_a_player_that_ignores_stop(bus):
    player = StubbornPlayer()
    sp, _, _ = make(bus, player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    assert timed(sp.speak, reply("Другое.")) < FAST
    assert timed(sp.stop) < FAST
    assert timed(sp.set_muted, True) < FAST
    sp.close()


def test_stop_mid_sentence_gives_idle_and_no_more_plays(bus):
    player = FakePlayer(block=True)
    sp, _, _ = make(bus, player=player)
    ev = Events(bus)
    sp.speak(reply(), "r1")
    assert player.started.wait(WAIT)
    t0 = time.monotonic()
    sp.stop()
    ev.wait(Status.IDLE, timeout=0.5)
    assert time.monotonic() - t0 < 0.5
    time.sleep(0.15)
    assert len(player.played) == 1
    assert ev.pairs() == [(Status.SPEAKING, "r1"), (Status.IDLE, "r1")]
    sp.stop()  # idempotent
    time.sleep(0.05)
    assert len(ev.all()) == 2
    sp.close()


def test_stop_when_idle_publishes_nothing(bus):
    sp, _, _ = make(bus)
    ev = Events(bus)
    sp.stop()
    sp.stop()
    time.sleep(0.05)
    assert ev.all() == []
    sp.close()


def test_stop_during_synthesis_discards_result(bus):
    tts = GatedTTS(gated=[FIRST])
    sp, _, player = make(bus, tts=tts)
    ev = Events(bus)
    sp.speak(reply(), "r1")
    assert tts.entered[FIRST].wait(WAIT)
    sp.stop()
    tts.release(FIRST)
    time.sleep(0.2)
    assert player.played == []
    assert ev.all() == []  # never spoke: no SPEAKING, no IDLE
    sp.close()


def test_stop_landing_just_before_play_starts_cuts_that_chunk(bus):
    gate = threading.Event()
    entered = threading.Event()

    def hold_first_play():
        if not entered.is_set():
            entered.set()
            gate.wait(WAIT)

    player = FakePlayer(block=True, on_enter=hold_first_play)
    sp, _, _ = make(bus, player=player)
    ev = Events(bus)
    sp.speak(reply(), "r1")
    assert entered.wait(WAIT)  # the worker is about to play; play() is not active yet
    sp.stop()
    assert wait_for(lambda: player.stop_count >= 2)  # first stop hit the idle player: retried
    gate.set()
    ev.wait(Status.IDLE, timeout=1.0)  # the chunk was cut at once, not played to the end
    time.sleep(0.1)
    assert len(player.played) == 1 and player.returned == 1
    assert len(ev.all()) == 2
    sp.close()


def test_late_stop_never_cuts_the_next_utterance(bus):
    player = FakePlayer(block=True)
    sp, _, _ = make(bus, player=player)
    for i in range(10):
        before = len(player.played)
        sp.speak(reply("Раз. Два."), f"a{i}")
        assert wait_for(lambda n=before: len(player.played) > n)
        before_b = len(player.played)
        returned = player.returned
        sp.speak(reply("Три. Четыре."), f"b{i}")  # old play is cut, the new one must stay
        assert wait_for(lambda n=before_b: len(player.played) > n)
        time.sleep(0.05)
        # exactly the old clip returned; the new utterance's first clip is still playing
        assert player.returned == returned + 1
        sp.stop()
        assert wait_for(lambda n=returned: player.returned == n + 2)
    sp.close()


def test_new_speak_supersedes_old_without_idle_blink(bus):
    player = FakePlayer(block=True)
    sp, tts, _ = make(bus, player=player)
    ev = Events(bus)
    sp.speak(reply(), "old")
    assert player.started.wait(WAIT)
    player.started.clear()
    sp.speak(reply("Новый ответ. Ещё один.", "ru"), "new")
    assert player.started.wait(WAIT)
    ev.wait(Status.SPEAKING, "new")
    player.finish()
    assert wait_for(lambda: len(player.played) == 3)
    player.finish()
    ev.wait(Status.IDLE, "new")
    assert ev.pairs() == [
        (Status.SPEAKING, "old"),
        (Status.SPEAKING, "new"),
        (Status.IDLE, "new"),
    ]
    assert "Третье предложение." not in [c.text for c in tts.calls]
    sp.close()


def test_rapid_speaks_play_only_the_last_utterance_fully(bus):
    sp, tts, _ = make(bus)
    ev = Events(bus)
    for i in range(20):
        sp.speak(reply("Раз. Два."), f"r{i}")
    ev.wait(Status.IDLE, "r19")
    assert tts.calls[-1].text == "Два."
    sp.close()


def test_rapid_speaks_never_run_two_syntheses_at_once(bus):
    tts = FakeTTS(delay=0.02)
    sp, _, _ = make(bus, tts=tts)
    ev = Events(bus)
    for i in range(20):
        sp.speak(reply("Раз. Два. Три."), f"r{i}")
        time.sleep(0.005)
    ev.wait(Status.IDLE, "r19")
    assert tts.max_concurrent == 1
    sp.close()


def test_muted_plays_nothing_and_publishes_nothing(bus):
    sp, tts, player = make(bus)
    ev = Events(bus)
    sp.set_muted(True)
    assert sp.muted
    sp.speak(reply(), "r1")
    time.sleep(0.15)
    assert tts.calls == [] and player.played == []
    assert ev.all() == []
    sp.set_muted(False)
    sp.speak(reply("Привет."), "r2")
    ev.wait(Status.IDLE)
    assert len(player.played) == 1
    sp.close()


def test_muting_stops_current_speech(bus):
    player = FakePlayer(block=True)
    sp, _, _ = make(bus, player=player)
    ev = Events(bus)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.set_muted(True)
    ev.wait(Status.IDLE, timeout=0.5)
    time.sleep(0.1)
    assert len(player.played) == 1
    sp.close()


def test_provider_error_publishes_error_then_idle_and_recovers(bus):
    tts = FakeTTS(error=ProviderUnavailable("boom"))
    sp, _, player = make(bus, tts=tts)
    ev = Events(bus)
    sp.speak(reply(), "r1")
    ev.wait(Status.IDLE)
    assert [(e.status, e.request_id, e.detail) for e in ev.all()] == [
        (Status.ERROR, "r1", "tts_failed"),
        (Status.IDLE, "r1", None),
    ]
    assert player.played == []
    tts.error = None
    sp.speak(reply("Привет."), "r2")
    ev.wait(Status.IDLE, "r2")
    assert [e.status for e in ev.all()][2:] == [Status.SPEAKING, Status.IDLE]
    sp.close()


def test_player_error_publishes_error_once_then_idle(bus):
    player = ErrorOnNthPlayer(error_on=2)
    sp, _, _ = make(bus, player=player)
    ev = Events(bus)
    sp.speak(reply(), "r1")
    assert player.started.wait(WAIT)
    player.finish()
    ev.wait(Status.IDLE)
    assert [e.status for e in ev.all()] == [Status.SPEAKING, Status.ERROR, Status.IDLE]
    assert ev.all()[1].detail == "tts_failed"
    assert len(player.played) == 2  # nothing after the failed clip
    sp.close()


def test_error_on_later_chunk_after_speaking(bus):
    class FlakyTTS(FakeTTS):
        def synthesize(self, text, lang):
            if text.startswith("Второе"):
                raise ProviderUnavailable("late")
            return super().synthesize(text, lang)

    sp, _, player = make(bus, tts=FlakyTTS())
    ev = Events(bus)
    sp.speak(reply())
    ev.wait(Status.IDLE)
    assert [e.status for e in ev.all()] == [Status.SPEAKING, Status.ERROR, Status.IDLE]
    assert len(player.played) == 1
    sp.close()


def test_error_of_a_superseded_utterance_is_not_reported(bus):
    class SlowFailTTS(FakeTTS):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.go = threading.Event()

        def synthesize(self, text, lang):
            if text == FIRST:
                self.entered.set()
                self.go.wait(WAIT)
                raise ProviderUnavailable("late failure")
            return super().synthesize(text, lang)

    tts = SlowFailTTS()
    sp, _, _ = make(bus, tts=tts)
    ev = Events(bus)
    sp.speak(reply(), "old")
    assert tts.entered.wait(WAIT)
    sp.stop()
    tts.go.set()
    time.sleep(0.2)
    assert ev.all() == []
    sp.close()


def test_empty_text_is_silent(bus):
    sp, tts, _ = make(bus)
    ev = Events(bus)
    sp.speak(reply("   "))
    time.sleep(0.1)
    assert tts.calls == [] and ev.all() == []
    sp.close()


def test_close_leaves_no_threads_and_closes_player(bus):
    before = len(worker_threads())
    player = FakePlayer(block=True)
    sp, _, _ = make(bus, player=player)
    sp.speak(reply())
    assert player.started.wait(WAIT)
    sp.close()
    assert wait_for(lambda: len(worker_threads()) == before)
    assert player.closed
    sp.close()  # idempotent
    sp.speak(reply())  # no-op after close
    assert len(player.played) == 1


def test_close_while_synthesis_is_blocked(bus):
    before = len(worker_threads())
    tts = GatedTTS(gated=[FIRST])
    sp, _, _ = make(bus, tts=tts)
    sp.speak(reply())
    assert tts.entered[FIRST].wait(WAIT)
    t0 = time.monotonic()
    sp.close(timeout=0.3)  # synthesis is still blocked: close gives up waiting for it
    assert time.monotonic() - t0 < 1.0
    tts.release(FIRST)
    assert wait_for(lambda: len(worker_threads()) == before)


def test_close_from_a_listener_thread_of_the_speaker_does_not_deadlock(bus):
    holder = {}

    class ClosingPlayer(FakePlayer):
        def play(self, clip):
            super().play(clip)
            holder["sp"].close(timeout=0.5)  # called on the worker thread itself

    sp, _, _ = make(bus, player=ClosingPlayer())
    holder["sp"] = sp
    sp.speak(reply("Привет."))
    assert wait_for(lambda: not sp._worker.is_alive())


def test_played_clips_are_audio_clips(bus):
    sp, _, player = make(bus)
    ev = Events(bus)
    sp.speak(reply("Привет."))
    ev.wait(Status.IDLE)
    assert isinstance(player.played[0], AudioClip)
    sp.close()


def test_unspeakable_chunk_is_skipped_and_the_rest_is_spoken(bus, caplog):
    class SkipTTS(FakeTTS):
        def synthesize(self, text, lang):
            if text.startswith("Второе"):
                raise NothingToSpeak("no character the voice can pronounce")
            return super().synthesize(text, lang)

    tts = SkipTTS()
    sp, _, player = make(bus, tts=tts)
    ev = Events(bus)
    sp.speak(reply(), "r1")
    ev.wait(Status.IDLE)
    assert [e.status for e in ev.all()] == [Status.SPEAKING, Status.IDLE]  # no ERROR
    assert len(player.played) == 2  # first and third
    assert [c.text for c in tts.calls][-1].startswith("Третье")
    sp.close()


def test_unspeakable_first_chunk_and_all_chunks(bus):
    class FirstSkipped(FakeTTS):
        def synthesize(self, text, lang):
            if text.startswith("Первое"):
                raise NothingToSpeak("x")
            return super().synthesize(text, lang)

    sp, _, player = make(bus, tts=FirstSkipped())
    ev = Events(bus)
    sp.speak(reply(), "r1")
    ev.wait(Status.IDLE)
    assert len(player.played) == 2
    assert Status.ERROR not in [e.status for e in ev.all()]
    sp.close()

    sp, _, player = make(bus, tts=FakeTTS(error=NothingToSpeak("x")))
    sp.speak(reply(), "r2")
    assert wait_for(lambda: not sp.active)
    assert player.played == []
    sp.close()


class WarmableTTS(FakeTTS):
    def __init__(self, error=None):
        super().__init__()
        self.warm_calls = 0
        self.error = error
        self.warmed = threading.Event()

    def warm_up(self):
        self.warm_calls += 1
        self.warmed.set()
        if self.error:
            raise self.error


def test_warmup_runs_once_in_background(bus):
    tts = WarmableTTS()
    sp, _, _ = make(bus, tts)
    sp.start_warmup()
    sp.start_warmup()  # idempotent
    assert tts.warmed.wait(WAIT)
    time.sleep(0.1)
    assert tts.warm_calls == 1
    sp.close()


def test_warmup_does_not_block_the_caller(bus):
    class Slow(WarmableTTS):
        def warm_up(self):
            time.sleep(1.0)
            super().warm_up()

    sp, _, _ = make(bus, Slow())
    assert timed(sp.start_warmup) < FAST
    sp.close()


def test_warmup_errors_are_swallowed(bus):
    tts = WarmableTTS(error=RuntimeError("boom"))
    sp, _, player = make(bus, tts)
    sp.start_warmup()
    assert tts.warmed.wait(WAIT)
    sp.speak(reply(FIRST))  # still works
    wait_for(lambda: len(player.played) == 1)
    sp.close()


def test_no_warmup_thread_for_a_provider_without_warm_up(bus):
    sp, _, _ = make(bus)  # FakeTTS (like SAPI) has no warm_up
    sp.start_warmup()
    assert not [t for t in worker_threads() if t.name.endswith("warm")]
    sp.close()


def test_no_warmup_while_muted(bus):
    tts = WarmableTTS()
    sp, _, _ = make(bus, tts)
    sp.set_muted(True)
    sp.start_warmup()
    time.sleep(0.2)
    assert tts.warm_calls == 0
    sp.close()


def test_warmup_repeats_after_a_long_idle(bus, monkeypatch):
    from luna.tts import speaker as speaker_mod

    monkeypatch.setattr(speaker_mod, "_WARM_CHECK_S", 0.05)
    monkeypatch.setattr(speaker_mod, "WARM_IDLE_S", 0.15)
    tts = WarmableTTS()
    sp, _, _ = make(bus, tts)
    sp.start_warmup()
    wait_for(lambda: tts.warm_calls >= 2)
    sp.close()


def test_first_audio_at_is_set_when_playing_starts(bus):
    sp, _, player = make(bus)
    assert sp.first_audio_at is None
    sp.speak(reply(FIRST))
    wait_for(lambda: sp.first_audio_at is not None)
    wait_for(lambda: len(player.played) == 1)
    sp.speak(reply(FIRST))  # a new utterance resets it
    sp.close()
