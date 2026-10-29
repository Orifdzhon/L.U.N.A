"""UI text catalog, Russian and English (ADR 0011 D9). Pure: no Qt.

``t(key, lang)`` looks a key up; ``lang`` is ``"ru"`` or ``"en"`` (an unknown language falls
back to English). ``resolve_language`` turns ``[ui] language`` (``"auto" | "ru" | "en"``)
into one of the two.
"""

# ruff: noqa: RUF001
from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Literal

Lang = Literal["ru", "en"]
DEFAULT_LANG: Lang = "ru"
_LANG_RUSSIAN = 0x19  # primary language id of Russian (winnt.h LANG_RUSSIAN)

_CATALOG: dict[str, dict[Lang, str]] = {
    # window
    "window.title": {"ru": "L.U.N.A.", "en": "L.U.N.A."},
    "window.input_placeholder": {
        "ru": "Напишите команду…",
        "en": "Type a command and press Enter",
    },
    "window.send": {"ru": "Отправить", "en": "Send"},
    "window.mic": {"ru": "Говорить", "en": "Push to talk"},
    "window.mic_unavailable": {
        "ru": "Голосовой ввод недоступен",
        "en": "Voice input is not available",
    },
    "window.prefix.user": {"ru": "Вы: ", "en": "You: "},
    "window.prefix.assistant": {"ru": "Луна: ", "en": "L.U.N.A.: "},
    # orb state captions
    "state.idle": {"ru": "Отдыхаю", "en": "Idle"},
    "state.wake": {"ru": "Слушаю «Луна»…", "en": "Listening for «Луна»…"},
    "state.recording": {"ru": "Слушаю…", "en": "Listening…"},
    "state.thinking": {"ru": "Думаю…", "en": "Thinking…"},
    "state.speaking": {"ru": "Говорю…", "en": "Speaking…"},
    "state.error": {"ru": "Ошибка", "en": "Error"},
    # status line and shell messages
    "status.idle": {"ru": "ожидание", "en": "idle"},
    "status.listening": {"ru": "слушаю", "en": "listening"},
    "status.thinking": {"ru": "думаю", "en": "thinking"},
    "status.speaking": {"ru": "говорю", "en": "speaking"},
    "status.error": {"ru": "ошибка", "en": "error"},
    "status.error_detail": {"ru": "ошибка: {detail}", "en": "error: {detail}"},
    "status.wake": {"ru": "Слово: {state}", "en": "Wake: {state}"},
    "status.conversation": {"ru": "диалог", "en": "conversation"},
    "status.muted": {"ru": "Голос: выкл", "en": "Voice: muted"},
    "status.llm": {"ru": "LLM: {name}", "en": "LLM: {name}"},
    "status.llm_offline": {"ru": "LLM: офлайн", "en": "LLM: offline"},
    # tray tooltips
    "tooltip.idle": {"ru": "L.U.N.A. - ожидание", "en": "L.U.N.A. - idle"},
    "tooltip.wake": {
        "ru": "L.U.N.A. - жду слово «Луна»",
        "en": "L.U.N.A. - listening for the wake word",
    },
    "tooltip.recording": {"ru": "L.U.N.A. - слушаю", "en": "L.U.N.A. - listening"},
    "tooltip.thinking": {"ru": "L.U.N.A. - думаю", "en": "L.U.N.A. - thinking"},
    "tooltip.speaking": {"ru": "L.U.N.A. - говорю", "en": "L.U.N.A. - speaking"},
    "tooltip.error": {"ru": "L.U.N.A. - ошибка", "en": "L.U.N.A. - error"},
    # tray menu
    "tray.wake": {"ru": "Слушать «Луна»", "en": "Listen for «Луна»"},
    "tray.wake_starting": {"ru": "Запуск слова пробуждения…", "en": "Starting wake word…"},
    "tray.voice": {"ru": "Голосовые ответы", "en": "Voice replies"},
    "tray.open": {"ru": "Открыть окно", "en": "Open window"},
    "tray.autostart": {"ru": "Запускать с Windows", "en": "Start with Windows"},
    "tray.keys": {"ru": "API-ключи…", "en": "API keys…"},
    "tray.quit": {"ru": "Выход", "en": "Quit"},
    # keys dialog
    "keys.title": {"ru": "L.U.N.A. - API-ключи", "en": "L.U.N.A. - API keys"},
    "keys.intro": {
        "ru": "Облачные ключи для голосового помощника (хранятся в Windows).",
        "en": "Cloud keys for the voice assistant (stored in Windows).",
    },
    "keys.required": {"ru": " - обязателен", "en": " - required"},
    "keys.placeholder": {"ru": "вставьте ключ сюда", "en": "paste the key here"},
    "keys.test": {"ru": "Проверить", "en": "Test"},
    "keys.save": {"ru": "Сохранить", "en": "Save"},
    "keys.later": {"ru": "Позже", "en": "Later"},
    "keys.enter_first": {"ru": "Сначала введите ключ", "en": "Enter a key first"},
    "keys.testing": {"ru": "Проверка...", "en": "Testing..."},
    "keys.saved": {
        "ru": "Сохранено. Перезапустите L.U.N.A., чтобы применить новый ключ.",
        "en": "Saved. Restart L.U.N.A. to use the new key.",
    },
    "keys.nothing": {"ru": "Нечего сохранять", "en": "Nothing to save"},
    "keys.save_failed": {"ru": "Не удалось сохранить: {error}", "en": "Could not save: {error}"},
}


def t(key: str, lang: str = DEFAULT_LANG, **fields: object) -> str:
    """Text for ``key`` in ``lang`` (English if ``lang`` is unknown); ``{name}`` placeholders."""
    entry = _CATALOG[key]
    text = entry["ru"] if lang == "ru" else entry["en"]
    return text.format(**fields) if fields else text


def catalog() -> dict[str, dict[Lang, str]]:
    return _CATALOG


def windows_ui_language_is_russian() -> bool:
    """True when the Windows UI language is Russian (False elsewhere or on any error)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return (int(lang_id) & 0x3FF) == _LANG_RUSSIAN


def resolve_language(
    setting: str, probe: Callable[[], bool] = windows_ui_language_is_russian
) -> Lang:
    """``"ru"``/``"en"`` as is; ``"auto"`` -> Russian if ``probe()`` says so, else English."""
    if setting == "ru":
        return "ru"
    if setting == "en":
        return "en"
    return "ru" if probe() else "en"
