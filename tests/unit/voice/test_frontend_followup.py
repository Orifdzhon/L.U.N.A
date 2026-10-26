"""Conversation mode (#113): the follow-up window after a reply (fakes only)."""

# ruff: noqa: RUF001  (Russian test phrases)

from __future__ import annotations

import pytest
import test_frontend_wake as base
from test_frontend_wake import CANCELLED, NO_SPEECH, FakeOrchestrator, ok, wait_for, wake

from fakes import FakeSTT
from luna.config.settings import WakeWordSettings
from luna.core.errors import ProviderUnavailable
from luna.core.events import Status

make = base.make  # the Harness factory fixture

CONV = WakeWordSettings(follow_up_s=90.0)


def settled(h, replies: int) -> None:
    wait_for(lambda: len(h.replies) >= replies)
    h.idle()


def finished(h) -> None:
    """The (last) turn ended and nothing else is running."""
    h.idle()
    wait_for(lambda: all(not t.is_alive() for t in h.fe._threads))


def test_window_opens_after_the_reply_with_the_full_length(make):
    h = make(wake=CONV, stt=FakeSTT([ok()]))
    assert wake(h) is True
    settled(h, 1)
    assert h.live.windows == [90.0]
    h.assert_holds_balanced()


def test_window_is_requested_while_the_turn_hold_is_still_set(make):
    h = make(wake=CONV, stt=FakeSTT([ok()]))
    seen: list[bool] = []
    original = h.live.open_window

    def spy(seconds: float) -> None:
        seen.append(bool(h.live.holds))
        original(seconds)

    h.live.open_window = spy  # type: ignore[method-assign]
    wake(h)
    settled(h, 1)
    assert seen == [True]  # half-duplex: the listener starts the clock after the holds


def test_rolling_restart_after_every_follow_up_turn(make):
    h = make(wake=CONV, stt=FakeSTT([ok("one"), ok("two"), ok("three"), ok("four"), ok("five")]))
    assert wake(h) is True
    settled(h, 1)
    for n in range(2, 6):  # no cap on chained follow-ups
        assert wake(h, follow_up=True) is True
        settled(h, n)
    assert h.live.windows == [90.0] * 5
    assert [r.text for r in h.orch.requests] == ["one", "two", "three", "four", "five"]


def test_follow_up_s_zero_keeps_the_old_behaviour(make):
    h = make(wake=WakeWordSettings(follow_up_s=0.0), stt=FakeSTT([ok("спасибо")]))
    wake(h)
    settled(h, 1)
    assert h.live.windows == []  # never asked, and "спасибо" is just a command
    assert [r.text for r in h.orch.requests] == ["спасибо"]


@pytest.mark.parametrize(
    "phrase",
    [
        "Спасибо",
        "стоп!",
        "Хватит.",
        "Всё",
        "все",
        "отбой",
        "Луна, отбой",
        "отбой, Луна",
        "Stop",
        "Thanks",
        "that's all",
        "Thank you",
    ],
)
def test_end_phrase_closes_silently_without_an_llm_call(make, phrase):
    h = make(wake=CONV, stt=FakeSTT([ok(phrase)]))
    wake(h, follow_up=True)
    wait_for(lambda: h.stt._script == [])
    finished(h)
    assert h.orch.requests == [] and h.replies == []
    assert h.live.windows == []  # no new window: back to the wake word
    statuses = h.statuses()
    assert Status.ERROR not in statuses and statuses[-1] is Status.IDLE


def test_end_phrase_after_the_keyword_in_a_wake_turn(make):
    h = make(wake=CONV, stt=FakeSTT([ok("Луна, спасибо")]))
    wake(h)
    wait_for(lambda: h.stt._script == [])
    finished(h)
    assert h.orch.requests == [] and h.live.windows == []


def test_end_phrase_inside_a_longer_command_is_a_command(make):
    h = make(wake=CONV, stt=FakeSTT([ok("спасибо, открой браузер")]))
    wake(h, follow_up=True)
    settled(h, 1)
    assert len(h.orch.requests) == 1 and h.live.windows == [90.0]


@pytest.mark.parametrize("text", ["", "   ", "я", "угу", "Ага.", "м-м", "Мм... ну", "э-э"])
def test_noise_and_fillers_are_ignored_without_a_reply(make, text):
    h = make(wake=CONV, stt=FakeSTT([ok("first"), ok(text)]))
    wake(h)
    settled(h, 1)
    h.events()
    assert wake(h, follow_up=True) is True
    wait_for(lambda: h.stt._script == [])
    finished(h)
    assert len(h.orch.requests) == 1 and len(h.replies) == 1
    assert Status.ERROR not in h.statuses()
    assert len(h.live.windows) == 2 and h.live.windows[0] == 90.0
    assert 0 < h.live.windows[1] <= 90.0  # the window comes back with the time it has left


def test_silence_in_a_follow_up_turn_gives_the_window_back_quietly(make):
    h = make([NO_SPEECH], wake=CONV, stt=FakeSTT([]))
    h.fe._conv_until = h.fe._clock() + 40
    wake(h, follow_up=True)
    finished(h)
    assert len(h.live.windows) == 1 and 0 < h.live.windows[0] <= 40
    assert h.replies == [] and Status.ERROR not in h.statuses()


def test_expired_conversation_is_not_reopened(make):
    h = make([NO_SPEECH], wake=CONV, stt=FakeSTT([]))
    h.fe._conv_until = h.fe._clock() - 1
    wake(h, follow_up=True)
    finished(h)
    assert h.live.windows == [] and h.fe._conv_until is None


def test_wake_turn_with_no_speech_does_not_open_a_window(make):
    h = make([NO_SPEECH], wake=CONV, stt=FakeSTT([]))
    wake(h)
    finished(h)
    assert h.live.windows == []


def test_follow_up_turn_strips_the_address_word(make):
    h = make(wake=CONV, stt=FakeSTT([ok("Луна, который час")]))
    wake(h, follow_up=True)
    settled(h, 1)
    assert h.orch.requests[0].text == "который час"


def test_hotkey_during_the_window_closes_it_then_reopens_after_the_reply(make):
    h = make(wake=CONV, stt=FakeSTT([ok("hotkey command")]))
    h.hotkey.press()
    settled(h, 1)
    assert h.live.windows == [0, 90.0]  # closed on the press, rolled after the reply
    assert h.orch.requests[0].text == "hotkey command"


def test_wake_word_during_the_window_is_a_normal_wake_turn(make):
    h = make(wake=CONV, stt=FakeSTT([ok("one"), ok("two")]))
    wake(h, follow_up=True)
    settled(h, 1)
    assert wake(h) is True  # the keyword event: verified like any wake
    settled(h, 2)
    assert h.verifier.calls  # the second stage ran for the keyword turn
    assert h.live.windows == [90.0, 90.0]


def test_no_window_after_an_stt_error(make):
    h = make(wake=CONV, stt=FakeSTT([ProviderUnavailable("down")]))
    wake(h, follow_up=True)
    finished(h)
    assert h.live.windows == [] and h.replies == []


def test_no_window_after_an_orchestrator_error(make):
    h = make(wake=CONV, stt=FakeSTT([ok()]), orchestrator=FakeOrchestrator(RuntimeError("boom")))
    wake(h)
    finished(h)
    assert h.live.windows == [] and h.replies == []


def test_no_window_after_a_cancelled_turn(make):
    h = make([CANCELLED], wake=CONV, stt=FakeSTT([]))
    wake(h, follow_up=True)
    finished(h)
    assert h.live.windows == []


def test_no_window_when_the_turn_was_cancelled_while_listening(make):
    h = make(["block"], wake=CONV, stt=FakeSTT([]))
    wake(h, follow_up=True)
    wait_for(lambda: h.recorder.entered.is_set())
    h.fe.stop()
    finished(h)
    assert h.live.windows == []


def test_noise_check_does_not_touch_hotkey_turns(make):
    h = make(wake=CONV, stt=FakeSTT([ok("угу")]))
    h.hotkey.press()
    settled(h, 1)
    assert len(h.orch.requests) == 1  # a hotkey turn is always a command
