import logging
import queue
import threading
import time

import pytest

from luna.core.errors import ToolError
from luna.platform.base import HotkeyListener
from luna.platform.windows import hotkey as hk
from luna.platform.windows.hotkey import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    WM_HOTKEY,
    WindowsHotkeyListener,
    parse_hotkey,
)


class FakeUser32:
    def __init__(self, fail_with: int | None = None) -> None:
        self.fail_with = fail_with
        self.messages: queue.Queue[tuple[int, int, int]] = queue.Queue()
        self.registered: list[tuple[int, int, int]] = []
        self.unregistered: list[int] = []
        self.quit_thread_ids: list[int] = []
        self.loop_thread: threading.Thread | None = None

    def current_thread_id(self) -> int:
        return 4242

    def create_message_queue(self) -> None:
        self.loop_thread = threading.current_thread()

    def register_hotkey(self, hotkey_id: int, modifiers: int, vk: int) -> bool:
        if self.fail_with is not None:
            return False
        self.registered.append((hotkey_id, modifiers, vk))
        return True

    def unregister_hotkey(self, hotkey_id: int) -> None:
        self.unregistered.append(hotkey_id)

    def last_error(self) -> int:
        return self.fail_with or 0

    def get_message(self) -> tuple[int, int, int]:
        return self.messages.get()

    def post_quit(self, thread_id: int) -> None:
        self.quit_thread_ids.append(thread_id)
        self.messages.put((0, hk.WM_QUIT, 0))

    def press(self) -> None:
        self.messages.put((1, WM_HOTKEY, 1))


def make(fail_with: int | None = None) -> tuple[WindowsHotkeyListener, FakeUser32]:
    fake = FakeUser32(fail_with)
    return WindowsHotkeyListener(lambda: fake), fake


def wait_for(predicate, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def hotkey_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("luna-hotkey")]


# -- parser -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "mods", "vk"),
    [
        ("ctrl+alt+space", MOD_CONTROL | MOD_ALT, 0x20),
        ("shift+f9", MOD_SHIFT, 0x78),
        ("win+q", MOD_WIN, ord("Q")),
        ("Ctrl+Shift+L", MOD_CONTROL | MOD_SHIFT, ord("L")),
        (" ctrl + alt + 7 ", MOD_CONTROL | MOD_ALT, ord("7")),
        ("ctrl+alt+shift+f12", MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x7B),
        ("ctrl+f24", MOD_CONTROL, 0x87),
    ],
)
def test_parse_ok(text, mods, vk):
    parsed = parse_hotkey(text)
    assert (parsed.modifiers, parsed.vk) == (mods, vk)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "space",
        "ctrl+alt",
        "ctrl+ctrl+a",
        "ctrl+nokey",
        "ctrl++a",
        "ctrl+f25",
        "ctrl+f0",
        "ctrl+f01",
        "a+ctrl",
        "ctrl+ab",
    ],
)
def test_parse_invalid(text):
    with pytest.raises(ToolError):
        parse_hotkey(text)


# -- listener ---------------------------------------------------------------------------


def test_is_a_hotkey_listener():
    assert isinstance(WindowsHotkeyListener(), HotkeyListener)


def test_start_registers_with_norepeat_and_press_runs_off_main_thread():
    listener, fake = make()
    seen: queue.Queue[threading.Thread] = queue.Queue()
    listener.start("ctrl+alt+space", lambda: seen.put(threading.current_thread()))
    try:
        assert fake.registered == [(1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20)]
        fake.press()
        thread = seen.get(timeout=1)
        assert thread is not threading.main_thread()
        assert thread is not fake.loop_thread  # callback is off the message thread
    finally:
        listener.stop()


def test_stop_joins_quickly_unregisters_and_is_idempotent():
    listener, fake = make()
    listener.start("ctrl+alt+space", lambda: None)
    started = time.monotonic()
    listener.stop()
    assert time.monotonic() - started < 1
    assert fake.quit_thread_ids == [4242]
    assert fake.unregistered == [1]
    assert not hotkey_threads()
    listener.stop()
    assert fake.quit_thread_ids == [4242]


def test_already_registered_raises_and_leaves_no_thread():
    listener, _ = make(fail_with=ERROR_HOTKEY_ALREADY_REGISTERED)
    with pytest.raises(ToolError, match=r"ctrl\+alt\+space.*already taken.*\[voice\] hotkey"):
        listener.start("ctrl+alt+space", lambda: None)
    assert wait_for(lambda: not hotkey_threads())
    listener.stop()  # no-op


def test_other_register_failure_reports_code():
    listener, _ = make(fail_with=87)
    with pytest.raises(ToolError, match="87"):
        listener.start("ctrl+alt+space", lambda: None)
    assert wait_for(lambda: not hotkey_threads())


def test_invalid_hotkey_does_not_touch_win32():
    created = []
    listener = WindowsHotkeyListener(lambda: created.append(1))
    with pytest.raises(ToolError, match="invalid hotkey"):
        listener.start("space", lambda: None)
    assert created == []


def test_win32_unavailable_is_tool_error():
    def boom():
        raise OSError("no user32")

    with pytest.raises(ToolError, match="unavailable"):
        WindowsHotkeyListener(boom).start("ctrl+alt+space", lambda: None)


def test_start_twice_raises():
    listener, _ = make()
    listener.start("ctrl+alt+space", lambda: None)
    try:
        with pytest.raises(ToolError, match="already running"):
            listener.start("ctrl+alt+space", lambda: None)
    finally:
        listener.stop()


def test_restart_after_stop():
    listener, fake = make()
    calls = []
    listener.start("ctrl+alt+space", lambda: calls.append(1))
    listener.stop()
    listener.start("ctrl+alt+space", lambda: calls.append(2))
    try:
        fake.press()
        assert wait_for(lambda: calls == [2])
    finally:
        listener.stop()


def test_callback_exception_is_logged_and_next_press_works(caplog):
    listener, fake = make()
    calls: list[int] = []

    def on_press():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger=hk.log.name):
        listener.start("ctrl+alt+space", on_press)
        try:
            fake.press()
            fake.press()
            assert wait_for(lambda: len(calls) == 2)
        finally:
            listener.stop()
    assert "hotkey callback raised" in caplog.text


def test_unrelated_messages_are_ignored():
    listener, fake = make()
    calls = []
    listener.start("ctrl+alt+space", lambda: calls.append(1))
    try:
        fake.messages.put((1, 0x0400, 1))  # some other message
        fake.messages.put((1, WM_HOTKEY, 99))  # someone else's hotkey id
        fake.press()
        assert wait_for(lambda: calls == [1])
    finally:
        listener.stop()
    assert calls == [1]


def test_slow_callback_does_not_block_message_loop():
    listener, fake = make()
    gate = threading.Event()
    listener.start("ctrl+alt+space", gate.wait)
    try:
        for _ in range(3):
            fake.press()
        started = time.monotonic()
        # stop() must work even though the callback is still blocked
        gate.set()
        listener.stop()
        assert time.monotonic() - started < 1
    finally:
        gate.set()
        listener.stop()
