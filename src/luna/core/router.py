"""Router: LLM-free fast path for "open an app / a site" commands (ARCHITECTURE 4.1, step 2).

The Router only *recognises* a narrow command shape ``[Luna,] <verb> <target> [please]``
and executes it through ``ToolRegistry.execute`` (so validation, safety levels and the action
log apply). It tries ``open_website`` first, then ``open_app``. A wrong action is worse than
asking the LLM, so anything uncertain is "not handled": questions, negations, several intents,
extra content after the target, unknown targets, unsupported or failed lookups.

It also flags requests that need fresh facts (``force_search``) for the Assistant.
"""

# ruff: noqa: RUF001
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from luna.core.errors import Abandoned
from luna.core.lang import detect_lang
from luna.core.models import Reply, Request
from luna.llm.base import ToolCall
from luna.tools.base import ToolContext, ToolResult
from luna.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

SITE_TOOL = "open_website"
APP_TOOL = "open_app"
PROVIDER = "router"
MAX_TEXT_CHARS = 200
MAX_TARGET_WORDS = 4

_WAKE_RE = re.compile(r"^\s*(?:луна|luna)(?!\w)[\s,.!:;\-—]*", re.IGNORECASE)
_VERB_RE = re.compile(
    r"^(?:открой|открыть|запусти|запустить|включи|open|launch|start)(?!\w)\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_FILLERS = r"(?:пожалуйста|плиз|please|pls)"
# Politeness is removed only at the edges (start/end of the text and of the target), never
# inside a name, so an app called "... Please ..." is not mangled.
_LEAD_FILLER_RE = re.compile(rf"^{_FILLERS}(?!\w)[\s,;]*", re.IGNORECASE)
_TRAIL_FILLER_RE = re.compile(rf"[\s,;]*(?<!\w){_FILLERS}$", re.IGNORECASE)
_ANY_VERB_RE = re.compile(
    r"(?<!\w)(?:открой|открыть|запусти|запустить|включи|open|launch|start)(?!\w)", re.IGNORECASE
)
_CONTRACTED_NEGATION_RE = re.compile(r"n['’]t(?!\w)", re.IGNORECASE)  # don't, can't ...
_LEAD_NOISE_RE = re.compile(
    r"^(?:мне|для\s+меня|сайт|приложение|программу|the|app|application|website|site)(?!\w)\s*",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[\w\-]+")
_TRAIL_PUNCT = " \t\r\n.!…,;:\"'«»"

# Tokens that mean "this is not a plain command": negation, questions, vague or multiple
# requests. Matched as whole words anywhere in the text (contractions such as "don't" are
# caught by _CONTRACTED_NEGATION_RE, the word tokenizer splits them). Side effect: an app whose
# name contains one of these words ("Stop Motion Studio") never takes the fast path; it goes
# to the LLM, which is the safe direction.
_NOT_A_COMMAND = frozenset(
    {
        # negation / cancel
        "не", "ни", "нет", "никогда", "без", "no", "not", "never", "without",
        # questions
        "что", "кто", "как", "какой", "какая", "какое", "какие", "где", "когда", "почему",
        "зачем", "сколько", "куда", "откуда", "what", "who", "how", "which", "where", "when",
        "why",
        # vague targets
        "что-нибудь", "что-то", "чего-нибудь", "кого-нибудь", "something", "anything",
        "somewhere", "some",
        # several intents / other actions
        "и", "а", "но", "или", "потом", "затем", "после", "and", "then", "or", "but", "also",
        "найди", "поищи", "покажи", "расскажи", "search", "find", "show", "tell",
        "закрой", "закрыть", "выключи", "удали", "close", "stop", "kill", "delete",
    }
)  # fmt: skip

_FRESH_FACTS_RE = re.compile(
    r"(?<!\w)(?:"
    r"курс(?:а|у|е|ы|ом|ов)?|цен(?:а|ы|у|е|ой)?|погод(?:а|ы|у|е|ой)|сегодня|сейчас"
    r"|новост(?:и|ь|ей|ям|ях)|prices?|rates?|weather|today|news"
    r")(?!\w)",
    re.IGNORECASE,
)

_TEMPLATES = {
    "ru": {
        "opening": "Открываю {name}.",
        "failed": "Не удалось открыть {name}.",
        "ambiguous": "Нашла несколько вариантов: {names}. Какой открыть?",
    },
    "en": {
        "opening": "Opening {name}.",
        "failed": "Could not open {name}.",
        "ambiguous": "I found several matches: {names}. Which one should I open?",
    },
}


DATETIME_TOOL = "get_datetime"
_RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_RU_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
_TIME_Q = (
    r"(?:скажи\s+|подскажи\s+)?(?:сколько\s+(?:сейчас\s+)?времени(?:\s+сейчас)?"
    r"|который\s+(?:сейчас\s+)?час|какое\s+(?:сейчас\s+)?время(?:\s+сейчас)?"
    r"|what\s+time\s+is\s+it(?:\s+now)?|what(?:'s|\s+is)\s+the\s+time(?:\s+now)?"
    r"|tell\s+me\s+the\s+time|what\s+time\s+is\s+it\s+right\s+now)"
)
_DATE_Q = (
    r"(?:скажи\s+|подскажи\s+)?(?:какое\s+(?:сегодня\s+)?число(?:\s+сегодня)?"
    r"|какой\s+(?:сегодня\s+)?день(?:\s+(?:недели|сегодня))?"
    r"|какая\s+(?:сегодня\s+)?дата(?:\s+сегодня)?"
    r"|what(?:'s|\s+is)\s+(?:the\s+|today'?s\s+)?(?:date|day)(?:\s+today)?"
    r"|what\s+day\s+is\s+it(?:\s+today)?|what\s+is\s+today'?s\s+date)"
)
_TIME_Q_RE = re.compile(_TIME_Q, re.IGNORECASE)
_DATE_Q_RE = re.compile(_DATE_Q, re.IGNORECASE)


def parse_datetime_question(text: str) -> str | None:
    """``"time"`` / ``"date"`` for a plain "what time / date is it" question, else ``None``.

    The whole text must be the question (after the wake word and politeness are removed), so
    "сколько времени лететь до Москвы" is left to the LLM.
    """
    if not text or len(text) > MAX_TEXT_CHARS:
        return None
    body = clean_query(text).strip(_TRAIL_PUNCT + "?").strip()
    if _TIME_Q_RE.fullmatch(body):
        return "time"
    if _DATE_Q_RE.fullmatch(body):
        return "date"
    return None


def _datetime_reply(kind: str, lang: str, result: ToolResult) -> Reply | None:
    data = result.data
    try:
        day = dt.date.fromisoformat(data["date"])
        clock = str(data["time"])[:5]
        weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        wd = weekday.index(data["weekday"])
    except (KeyError, ValueError, TypeError):
        return None
    if lang == "ru":
        when = f"{_RU_WEEKDAYS[wd]}, {day.day} {_RU_MONTHS[day.month - 1]}"
        text = f"Сейчас {clock}, {when}." if kind == "time" else f"Сегодня {when}."
    else:
        when = f"{data['weekday']}, {_EN_MONTHS[day.month - 1]} {day.day}"
        text = f"It's {clock}, {when}." if kind == "time" else f"Today is {when}."
    return Reply(text, lang, [result], PROVIDER)


@dataclass(frozen=True)
class RouteResult:
    handled: bool
    reply: Reply | None = None  # set only when handled
    force_search: bool = False  # the request needs fresh facts: the LLM must search first


class _DenyConfirmer:
    """The router runs SAFE tools only; any confirmation request is refused."""

    def confirm(self, prompt: str) -> bool:
        return False


def needs_fresh_facts(text: str) -> bool:
    """Fresh-facts keywords present and no open/launch verb ("сейчас открой хром" is a command)."""
    return _FRESH_FACTS_RE.search(text) is not None and _ANY_VERB_RE.search(text) is None


def parse_open_command(text: str) -> str | None:
    """Return the target of a plain ``[Luna,] <verb> <target> [please]`` command, else ``None``."""
    if not text or len(text) > MAX_TEXT_CHARS or "?" in text:
        return None
    body = _WAKE_RE.sub("", text, count=1)
    body = _strip_fillers(body)
    match = _VERB_RE.match(body)
    if match is None:
        return None
    target = _strip_fillers(match.group(1))
    while (noise := _LEAD_NOISE_RE.match(target)) is not None and noise.end() > 0:
        target = _strip_fillers(target[noise.end() :])
    if not target or "," in target or ";" in target:
        return None
    words = [w.casefold() for w in _WORD_RE.findall(body)]
    if _CONTRACTED_NEGATION_RE.search(body) or any(w in _NOT_A_COMMAND for w in words):
        return None
    if len(target.split()) > MAX_TARGET_WORDS:
        return None
    return target


def clean_query(text: str) -> str:
    """Text without the wake word and edge politeness (for search queries)."""
    return _strip_fillers(_WAKE_RE.sub("", text, count=1))


def _strip_fillers(text: str) -> str:
    """Drop politeness words (and punctuation) at both edges of ``text``."""
    prev = None
    text = text.strip(_TRAIL_PUNCT)
    while prev != text:
        prev = text
        text = _LEAD_FILLER_RE.sub("", text).strip(_TRAIL_PUNCT)
        text = _TRAIL_FILLER_RE.sub("", text).strip(_TRAIL_PUNCT)
    return text


class Router:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._confirmer = _DenyConfirmer()

    def route(self, request: Request, *, cancel: threading.Event | None = None) -> RouteResult:
        text = request.text if isinstance(request.text, str) else ""
        fallthrough = RouteResult(False, force_search=needs_fresh_facts(text))
        kind = parse_datetime_question(text)
        if kind is not None:
            return self._datetime(request, kind, cancel) or fallthrough
        target = parse_open_command(text)
        if target is None:
            return fallthrough
        try:
            result = self._execute(request, target, cancel)
        except Abandoned:
            return fallthrough  # abandoned before any tool ran
        except Exception:  # the registry does not raise; this keeps the LLM path reachable
            logger.exception("router fast path failed unexpectedly")
            result = None
        return result or fallthrough

    def _datetime(
        self, request: Request, kind: str, cancel: threading.Event | None
    ) -> RouteResult | None:
        lang = detect_lang(request.text, request.lang)
        ctx = ToolContext(
            request_id=request.request_id,
            source=request.source,
            confirmer=self._confirmer,
            user_text=request.text,
            provider=PROVIDER,
        )
        try:
            result = self._call(DATETIME_TOOL, {}, ctx, 1, cancel)
        except Abandoned:
            return None
        except Exception:
            logger.exception("router date/time path failed unexpectedly")
            return None
        reply = _datetime_reply(kind, lang, result) if result.ok else None
        return RouteResult(True, reply) if reply is not None else None

    def _execute(
        self, request: Request, target: str, cancel: threading.Event | None
    ) -> RouteResult | None:
        lang = detect_lang(request.text, request.lang)
        ctx = ToolContext(
            request_id=request.request_id,
            source=request.source,
            confirmer=self._confirmer,
            user_text=request.text,
            provider=PROVIDER,
        )
        site = self._call(SITE_TOOL, {"target": target}, ctx, 1, cancel)
        if site.ok:
            return _reply(lang, _display_name(target), [site])
        reason = _reason(site)
        if reason == "open_failed":
            return _reply(lang, _display_name(target), [site], failed=True)
        if reason != "no_match":
            return None  # unsafe target, bad arguments, tool missing: let the LLM decide

        app = self._call(APP_TOOL, {"name": target}, ctx, 2, cancel)
        if app.ok:
            name = app.data.get("app")
            return _reply(lang, name if isinstance(name, str) else target, [site, app])
        reason = _reason(app)
        if reason == "open_failed":
            return _reply(lang, _display_name(target), [site, app], failed=True)
        if reason == "ambiguous":
            candidates = app.data.get("candidates")
            if isinstance(candidates, list) and candidates:
                names = ", ".join(str(c) for c in candidates)
                text = _TEMPLATES[lang]["ambiguous"].format(names=names)
                return RouteResult(True, Reply(text, lang, [site, app], PROVIDER))
        return None

    def _call(
        self,
        tool: str,
        args: dict[str, Any],
        ctx: ToolContext,
        n: int,
        cancel: threading.Event | None,
    ) -> ToolResult:
        if cancel is not None and cancel.is_set():
            raise Abandoned
        call = ToolCall(id=f"router-{ctx.request_id}-{n}", name=tool, arguments=args)
        return self._registry.execute(call, ctx)


def _reply(lang: str, name: str, actions: list[ToolResult], *, failed: bool = False) -> RouteResult:
    text = _TEMPLATES[lang]["failed" if failed else "opening"].format(name=name)
    return RouteResult(True, Reply(text, lang, actions, PROVIDER))


def _reason(result: ToolResult) -> str | None:
    reason = result.data.get("reason") if isinstance(result.data, dict) else None
    return reason if isinstance(reason, str) else None


_SITE_NAMES = {
    **dict.fromkeys(["youtube", "ютуб", "ютюб"], "YouTube"),
    **dict.fromkeys(["github", "гитхаб", "гитхап"], "GitHub"),
    **dict.fromkeys(["google", "гугл"], "Google"),
    **dict.fromkeys(["habr", "хабр", "хабрахабр"], "Habr"),
    **dict.fromkeys(["vk", "вк", "вконтакте"], "VK"),
    **dict.fromkeys(["wikipedia", "википедия"], "Wikipedia"),
    **dict.fromkeys(["yandex", "яндекс"], "Yandex"),
    **dict.fromkeys(["gmail", "гмейл"], "Gmail"),
    **dict.fromkeys(["twitch", "твич"], "Twitch"),
    **dict.fromkeys(["reddit"], "Reddit"),
}


def _display_name(target: str) -> str:
    """Canonical name for known site aliases, else the target (first letter capitalized)."""
    known = _SITE_NAMES.get(target.casefold().replace("ё", "е"))
    if known:
        return known
    if target.islower() and "." not in target and "/" not in target:
        return target[:1].upper() + target[1:]
    return target
