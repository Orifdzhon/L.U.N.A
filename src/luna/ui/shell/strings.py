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
    "tray.keys": {"ru": "Модели / провайдеры…", "en": "Models / providers…"},
    "tray.guide": {"ru": "Как пользоваться", "en": "How to use"},
    "tray.quit": {"ru": "Выход", "en": "Quit"},
    # first-run guide
    "guide.title": {"ru": "Как пользоваться Луной", "en": "How to use L.U.N.A."},
    "guide.call": {
        "ru": "Позовите: скажите «Луна, …», нажмите кнопку микрофона или напишите команду внизу.",
        "en": "Call her: say «Луна, …», press the mic button or type a command below.",
    },
    "guide.examples": {
        "ru": "Например: «открой ютуб», «найди файл отчёт», «закрой телеграм», «какая погода».",
        "en": "For example: «open youtube», «find file report», «close telegram», «weather».",
    },
    "guide.conversation": {
        "ru": "Диалог: после ответа продолжайте без слова «Луна». «Спасибо» завершает разговор.",
        "en": "Conversation: after a reply keep talking, no wake word. «Thanks» ends it.",
    },
    "guide.settings": {
        "ru": "Настройки: значок в трее → «Модели / провайдеры…».",
        "en": "Settings: tray icon → «Models / providers…».",
    },
    "guide.again": {
        "ru": "Эту подсказку можно открыть снова: значок в трее → «Как пользоваться».",
        "en": "Open this guide again: tray icon → «How to use».",
    },
    "guide.ok": {"ru": "Понятно", "en": "Got it"},
    # models / providers dialog
    "providers.title": {
        "ru": "L.U.N.A. - Модели / провайдеры",
        "en": "L.U.N.A. - Models / providers",
    },
    "providers.order": {
        "ru": "Порядок (первый отвечает первым)",
        "en": "Order (the first one answers first)",
    },
    "providers.up": {"ru": "Вверх", "en": "Up"},
    "providers.down": {"ru": "Вниз", "en": "Down"},
    "providers.remove": {"ru": "Убрать", "en": "Remove"},
    "providers.add": {"ru": "Добавить провайдера", "en": "Add provider"},
    "providers.custom_item": {
        "ru": "Свой (совместимый с OpenAI)...",
        "en": "Custom (OpenAI-compatible)...",
    },
    "providers.custom_title": {
        "ru": "Свой провайдер (совместимый с OpenAI)",
        "en": "Custom provider (OpenAI-compatible)",
    },
    "providers.custom_name": {"ru": "Имя", "en": "Name"},
    "providers.custom_name_hint": {"ru": "например, myserver", "en": "e.g. myserver"},
    "providers.custom_add": {"ru": "Добавить", "en": "Add"},
    "providers.base_url": {"ru": "Адрес API", "en": "Base URL"},
    "providers.model": {"ru": "Модель", "en": "Model"},
    "providers.key": {"ru": "Ключ", "en": "Key"},
    "providers.key_placeholder": {"ru": "вставьте ключ сюда", "en": "paste the key here"},
    "providers.fetch": {"ru": "Список моделей", "en": "Fetch models"},
    "providers.get_key": {"ru": "Получить ключ", "en": "Get a key"},
    "providers.test": {"ru": "Проверить", "en": "Test"},
    "providers.save": {"ru": "Сохранить", "en": "Save"},
    "providers.close": {"ru": "Закрыть", "en": "Close"},
    "providers.restart": {"ru": "Перезапустить L.U.N.A.", "en": "Restart L.U.N.A."},
    "providers.testing": {"ru": "Проверка...", "en": "Testing..."},
    "providers.enter_key": {"ru": "Сначала введите ключ", "en": "Enter a key first"},
    "providers.enter_model": {
        "ru": "Сначала введите название модели",
        "en": "Enter a model name first",
    },
    "providers.saved": {
        "ru": "Сохранено. Новые настройки применятся после перезапуска.",
        "en": "Saved. The new setup applies after a restart.",
    },
    "providers.cannot_add": {"ru": "Не удалось добавить: {error}", "en": "Cannot add: {error}"},
    "providers.url_error": {"ru": "Адрес API: {error}", "en": "Base URL: {error}"},
    "providers.cannot_save": {
        "ru": "Не удалось сохранить: {error}",
        "en": "Could not save: {error}",
    },
    "providers.cannot_save_key": {
        "ru": "Не удалось сохранить ключ: {error}",
        "en": "Could not save the key: {error}",
    },
    "providers.order_hint": {
        "ru": "luna.toml тоже задаёт порядок; порядок из этого окна главнее.",
        "en": "luna.toml also sets the order; the dialog's order wins.",
    },
    "providers.stt_note": {
        "ru": "Распознавание речи использует Groq - оставьте ключ Groq, "
        "даже если отвечает другая модель.",
        "en": "Speech recognition uses Groq - keep the Groq key even if another model answers.",
    },
    "providers.warn.no_tools": {
        "ru": "Эта модель не умеет вызывать инструменты: она сможет только отвечать, "
        "но не открывать приложения и не искать в интернете.",
        "en": "This model cannot call tools: it can only answer, not open apps or search the web.",
    },
    "providers.warn.paid": {
        "ru": "Платный провайдер: запросы оплачиваются с вашего баланса.",
        "en": "Paid provider: requests are charged to your balance.",
    },
    "providers.warn.trial": {
        "ru": "Пробный период: после его окончания запросы станут платными.",
        "en": "Trial: requests become paid when the trial ends.",
    },
    "providers.warn.untested": {
        "ru": "Поддержка инструментов не проверена - нажмите «Проверить».",
        "en": "Tool support is not tested - press Test.",
    },
    "providers.badge.yes": {"ru": "бесплатно", "en": "free"},
    "providers.badge.trial": {"ru": "пробный период", "en": "trial"},
    "providers.badge.paid": {"ru": "платно", "en": "paid"},
    "providers.badge.local": {"ru": "локально", "en": "local"},
    "providers.key.none": {"ru": "ключ не нужен", "en": "no key needed"},
    "providers.key.saved": {"ru": "сохранён", "en": "saved"},
    "providers.key.overridden": {
        "ru": "сохранён; его перекрывает ключ из окружения / .env",
        "en": "saved; the key from environment / .env overrides it",
    },
    "providers.key.env": {"ru": "задан в окружении / .env", "en": "set in environment / .env"},
    "providers.key.not_set": {"ru": "не задан", "en": "not set"},
    "providers.err.no_provider": {
        "ru": "Включите хотя бы одного провайдера",
        "en": "Enable at least one provider",
    },
    "providers.err.no_model": {
        "ru": "У провайдера {name} не указана модель",
        "en": "Provider {name} has no model name",
    },
    "providers.check.ok": {"ru": "Ключ принят", "en": "OK"},
    "providers.check.rejected": {"ru": "Ключ отклонён (401/403)", "en": "Rejected (401/403)"},
    "providers.check.no_connection": {"ru": "Нет соединения", "en": "No connection"},
    "providers.check.error": {"ru": "Неожиданный ответ", "en": "Unexpected answer"},
    "providers.tools.yes": {"ru": "инструменты: да", "en": "tools: yes"},
    "providers.tools.unclear": {
        "ru": "инструменты: неясно (модель ответила без вызова)",
        "en": "tools: unclear (the model answered without calling the tool)",
    },
    "providers.tools.no": {"ru": "инструменты: нет", "en": "tools: no"},
    "providers.models_count": {"ru": "моделей: {n}", "en": "{n} models"},
    "providers.latency": {"ru": "{ms} мс", "en": "{ms} ms"},
    "providers.name_rule": {
        "ru": "имя: 1-32 символа a-z 0-9 _, начинается с буквы",
        "en": "name: 1-32 characters, a-z 0-9 _, starting with a letter",
    },
    "providers.name_taken": {
        "ru": "провайдер с таким именем уже есть",
        "en": "a provider with this name already exists",
    },
    "providers.url_fixed": {
        "ru": "адрес API готового провайдера менять нельзя",
        "en": "the URL of a preset provider is fixed",
    },
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
