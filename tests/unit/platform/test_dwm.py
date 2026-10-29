"""Dark title bar helper: a no-op off Windows, and never raises."""

from types import SimpleNamespace

from luna.platform.windows import dwm


def _fake_windll(fn):
    return SimpleNamespace(dwmapi=SimpleNamespace(DwmSetWindowAttribute=fn))


def test_noop_off_windows(monkeypatch):
    monkeypatch.setattr(dwm.sys, "platform", "linux")
    assert dwm.enable_dark_title_bar(1234) is False


def test_zero_handle_is_ignored(monkeypatch):
    monkeypatch.setattr(dwm.sys, "platform", "win32")
    assert dwm.enable_dark_title_bar(0) is False


def test_calls_dwm_with_the_immersive_dark_attribute(monkeypatch):
    calls = []

    def fake(hwnd, attribute, value, size):
        calls.append((attribute.value, value._obj.value, size))
        return 0

    monkeypatch.setattr(dwm.sys, "platform", "win32")
    monkeypatch.setattr(dwm.ctypes, "windll", _fake_windll(fake), raising=False)
    assert dwm.enable_dark_title_bar(42) is True
    assert calls == [(20, 1, 4)]


def test_failure_returns_false(monkeypatch):
    def broken(*args):
        raise OSError("no dwm")

    monkeypatch.setattr(dwm.sys, "platform", "win32")
    monkeypatch.setattr(dwm.ctypes, "windll", _fake_windll(broken), raising=False)
    assert dwm.enable_dark_title_bar(42) is False
