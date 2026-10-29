"""UI strings catalog and the ``[ui] language`` setting."""

from __future__ import annotations

import re
import string

import pytest

from luna.config.settings import Settings
from luna.ui.shell import strings
from luna.ui.shell.state import IconState, tooltip_for
from luna.ui.shell.strings import catalog, resolve_language, t


def test_every_key_has_both_languages() -> None:
    for key, texts in catalog().items():
        assert set(texts) == {"ru", "en"}, key
        assert texts["ru"].strip() and texts["en"].strip(), key


def test_russian_texts_are_russian_and_placeholders_match() -> None:
    fields = string.Formatter()
    for key, texts in catalog().items():
        names_ru = {n for _, n, _, _ in fields.parse(texts["ru"]) if n}
        names_en = {n for _, n, _, _ in fields.parse(texts["en"]) if n}
        assert names_ru == names_en, key
    assert re.search("[Ѐ-ӿ]", t("tray.quit", "ru"))
    assert t("tray.quit", "en") == "Quit"


def test_unknown_language_falls_back_to_english_and_default_is_russian() -> None:
    assert t("tray.quit", "de") == "Quit"
    assert t("tray.quit") == t("tray.quit", "ru")


def test_placeholders_are_formatted() -> None:
    assert t("keys.save_failed", "en", error="boom") == "Could not save: boom"
    assert "boom" in t("keys.save_failed", "ru", error="boom")


def test_auto_uses_the_injected_probe() -> None:
    assert resolve_language("auto", lambda: True) == "ru"
    assert resolve_language("auto", lambda: False) == "en"
    assert resolve_language("ru", lambda: False) == "ru"
    assert resolve_language("en", lambda: True) == "en"


def test_default_probe_never_raises() -> None:
    assert isinstance(strings.windows_ui_language_is_russian(), bool)


def test_every_state_has_caption_and_tooltip_in_both_languages() -> None:
    for state in IconState:
        assert t(f"state.{state.value}", "ru") and t(f"state.{state.value}", "en")
        assert tooltip_for(state, "ru") != tooltip_for(state, "en")


def test_language_setting_default_and_validation() -> None:
    assert Settings().ui.language == "auto"
    assert Settings.model_validate({"ui": {"language": "ru"}}).ui.language == "ru"
    with pytest.raises(ValueError):
        Settings.model_validate({"ui": {"language": "fr"}})


def test_tray_uses_the_language() -> None:
    from PySide6.QtWidgets import QApplication

    from luna.ui.qt.tray import QtTrayView

    QApplication.instance() or QApplication([])
    noop = lambda *_a: None  # noqa: E731
    tray = QtTrayView(
        on_open=noop,
        on_set_wake=noop,
        on_set_voice=noop,
        on_set_autostart=noop,
        on_quit=noop,
        lang="ru",
    )
    assert tray.quit_action.text() == t("tray.quit", "ru")
    assert tray.keys_action.text() == t("tray.keys", "ru")
