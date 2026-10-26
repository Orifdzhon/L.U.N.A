# ruff: noqa: RUF001
"""Text normalisation for local neural voices (Piper): numbers, currency, brands, Latin words.

Pure functions, no I/O, no dependencies. Local voices read what they are given: "79 764 ₽" or
"YouTube" come out garbled, so the text is rewritten into words first. Online voices (Edge) and
SAPI do this themselves and get the original text.

Russian (``lang="ru"``): integers (with space-separated thousands) up to 10^12, simple decimals,
negatives, ``hh:mm`` times, ``₽ $ € %`` with the right noun form, a table of common brands and
abbreviations, spelled-out all-caps acronyms and a simple transliteration for other Latin words.
English: only the symbols ``₽ $ € %`` are expanded (the English voice reads digits itself).
"""

from __future__ import annotations

import re

__all__ = ["normalize_for_speech", "number_to_words"]

_ONES = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_ONES_F = ["ноль", "одна", "две", *_ONES[3:]]
_TEENS = [
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
    "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
]  # fmt: skip
_TENS = [
    "", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
    "восемьдесят", "девяносто",
]  # fmt: skip
_HUNDREDS = [
    "", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
    "восемьсот", "девятьсот",
]  # fmt: skip
# (one, few, many, feminine) per scale step above the units
_SCALES = [
    ("тысяча", "тысячи", "тысяч", True),
    ("миллион", "миллиона", "миллионов", False),
    ("миллиард", "миллиарда", "миллиардов", False),
]
MAX_INT_DIGITS = 12


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def _triple(n: int, feminine: bool) -> list[str]:
    words = [_HUNDREDS[n // 100]] if n >= 100 else []
    rest = n % 100
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
    else:
        if rest >= 20:
            words.append(_TENS[rest // 10])
        if rest % 10:
            words.append((_ONES_F if feminine else _ONES)[rest % 10])
    return [w for w in words if w]


def number_to_words(n: int, *, feminine: bool = False) -> str:
    """Russian cardinal in the nominative case; ``feminine`` for "одна/две" (last group)."""
    if n < 0:
        return "минус " + number_to_words(-n, feminine=feminine)
    if n == 0:
        return _ONES[0]
    groups: list[int] = []
    rest = n
    while rest:
        groups.append(rest % 1000)
        rest //= 1000
    if len(groups) > len(_SCALES) + 1:
        raise ValueError("number too large")
    words: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        value = groups[index]
        if not value:
            continue
        if index == 0:
            words += _triple(value, feminine)
        else:
            one, few, many, scale_fem = _SCALES[index - 1]
            words += _triple(value, scale_fem)
            words.append(_plural(value, one, few, many))
    return " ".join(words)


_FRACTION_NAMES = {
    1: ("десятая", "десятых"),
    2: ("сотая", "сотых"),
    3: ("тысячная", "тысячных"),
}


def _digits_aloud(digits: str) -> str:
    return " ".join(_ONES[int(d)] for d in digits)


def _decimal_to_words(whole: int, frac: str) -> str:
    """ "3,5" -> "три целых пять десятых" (the noun after it is genitive singular)."""
    head = number_to_words(whole, feminine=True)
    head += " целая" if whole % 10 == 1 and whole % 100 != 11 else " целых"
    names = _FRACTION_NAMES.get(len(frac))
    if names is None:  # long fraction: read the digits
        return f"{number_to_words(whole)} запятая {_digits_aloud(frac)}"
    numerator = int(frac)
    if numerator == 0:
        return number_to_words(whole)
    form = names[0] if numerator % 10 == 1 and numerator % 100 != 11 else names[1]
    return f"{head} {number_to_words(numerator, feminine=True)} {form}"


# --- Russian number / currency / percent patterns ---------------------------------------

_NUM = r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,9})?"
_SYMBOL_UNITS = {
    "₽": ("рубль", "рубля", "рублей"),
    "$": ("доллар", "доллара", "долларов"),
    "€": ("евро", "евро", "евро"),
    "%": ("процент", "процента", "процентов"),
}
_UNIT_AFTER = r"(?:\s?(?P<sym>[₽$€%])|\s(?P<word>руб(?:\.|лей|ля|ль)?)(?!\w))?"
_NUMBER_RE = re.compile(
    rf"(?P<cur>[$€₽]\s?)?(?<![\w.,])(?P<neg>[-−])?(?P<num>{_NUM})(?!\w)" + _UNIT_AFTER
)
_TIME_RE = re.compile(r"(?<![\w:])([01]?\d|2[0-3]):([0-5]\d)(?![\w:])")


def _spoken_number(raw: str) -> tuple[str, bool, int]:
    """(words, is_fractional, integer-part) for a matched number such as "79 764" / "3,5"."""
    compact = re.sub(r"[ \u00a0\u202f]", "", raw)
    whole_s, _, frac = compact.replace(",", ".").partition(".")
    whole = int(whole_s) if len(whole_s) <= MAX_INT_DIGITS else -1
    if whole < 0:
        return (
            _digits_aloud(whole_s) + (" запятая " + _digits_aloud(frac) if frac else ""),
            bool(frac),
            0,
        )
    if len(whole_s) > 1 and whole_s.startswith("0") and not frac:
        return _digits_aloud(whole_s), False, whole  # "007", "0123": read digit by digit
    if frac and int(frac):
        return _decimal_to_words(whole, frac), True, whole
    return number_to_words(whole), False, whole


def _number_sub(match: re.Match[str]) -> str:
    words, fractional, whole = _spoken_number(match.group("num"))
    sym = (match.group("cur") or "").strip() or match.group("sym")
    if sym:
        units: tuple[str, str, str] | None = _SYMBOL_UNITS[sym]
    else:
        units = _SYMBOL_UNITS["₽"] if match.group("word") else None
    if units is not None:
        words = f"{words} {units[1] if fractional else _plural(whole, *units)}"
    if match.group("neg"):
        words = "минус " + words
    return words


# --- years: "в 2024 году" needs an ordinal ---------------------------------------------------

_ORD_UNITS = [
    "", "первый", "второй", "третий", "четвёртый", "пятый", "шестой", "седьмой", "восьмой",
    "девятый", "десятый", "одиннадцатый", "двенадцатый", "тринадцатый", "четырнадцатый",
    "пятнадцатый", "шестнадцатый", "семнадцатый", "восемнадцатый", "девятнадцатый",
]  # fmt: skip
_ORD_TENS = [
    "", "", "двадцатый", "тридцатый", "сороковой", "пятидесятый", "шестидесятый",
    "семидесятый", "восьмидесятый", "девяностый",
]  # fmt: skip
_ORD_HUNDREDS = [
    "", "сотый", "двухсотый", "трёхсотый", "четырёхсотый", "пятисотый", "шестисотый",
    "семисотый", "восьмисотый", "девятисотый",
]  # fmt: skip
_YEAR_RE = re.compile(r"(?<![\w.,])([12]\d{3})(\s+)(год|года|году|годе)(?!\w)")
_YEAR_CASE = {"год": "nom", "года": "gen", "году": "prep", "годе": "prep"}


def _ordinal_case(word: str, case: str) -> str:
    if case == "nom":
        return word
    if word.endswith("ий"):
        return word[:-2] + ("ьего" if case == "gen" else "ьем")
    return word[:-2] + ("ого" if case == "gen" else "ом")


def _year_to_words(year: int, case: str) -> str:
    """ "2024" -> "две тысячи двадцать четвёртый" (nominative / genitive / prepositional)."""
    if year % 1000 == 0:
        last = {1000: "тысячный", 2000: "двухтысячный"}[year]
        return _ordinal_case(last, case)
    rest = year % 1000
    head = "тысяча" if year < 2000 else number_to_words(year - rest)
    if rest % 100 == 0:
        tail = _ORD_HUNDREDS[rest // 100]
        prefix = ""
    elif rest % 100 < 20:
        tail = _ORD_UNITS[rest % 100]
        prefix = number_to_words(rest - rest % 100) if rest >= 100 else ""
    elif rest % 10 == 0:
        tail = _ORD_TENS[rest % 100 // 10]
        prefix = number_to_words(rest - rest % 100) if rest >= 100 else ""
    else:
        tail = _ORD_UNITS[rest % 10]
        prefix = number_to_words(rest - rest % 10)
    return " ".join(w for w in (head, prefix, _ordinal_case(tail, case)) if w)


def _year_sub(match: re.Match[str]) -> str:
    year = int(match.group(1))
    if year % 1000 == 0 and year not in (1000, 2000):
        return match.group(0)
    return f"{_year_to_words(year, _YEAR_CASE[match.group(3)])} {match.group(3)}"


def _time_sub(match: re.Match[str]) -> str:
    hours, minutes = int(match.group(1)), int(match.group(2))
    head = number_to_words(hours)
    if minutes == 0:
        return f"{head} {_plural(hours, 'час', 'часа', 'часов')}"
    if minutes < 10:
        return f"{head} ноль {number_to_words(minutes)}"
    return f"{head} {number_to_words(minutes)}"


# --- Latin words in Russian text ------------------------------------------------------------

_BRANDS = {
    "youtube": "ютуб", "instagram": "инстаграм", "google": "гугл", "telegram": "телеграм",
    "whatsapp": "вотсап", "facebook": "фейсбук", "twitter": "твиттер", "tiktok": "тикток",
    "netflix": "нетфликс", "spotify": "спотифай", "discord": "дискорд", "steam": "стим",
    "chrome": "хром", "firefox": "файрфокс", "edge": "эдж", "windows": "виндоус",
    "microsoft": "майкрософт", "apple": "эпл", "iphone": "айфон", "android": "андроид",
    "amazon": "амазон", "yandex": "яндекс", "wifi": "вай-фай", "wi-fi": "вай-фай",
    "bluetooth": "блютус", "email": "имейл", "e-mail": "имейл", "online": "онлайн",
    "ok": "окей", "okay": "окей", "openai": "оупен-эй-ай", "chatgpt": "чат-джи-пи-ти",
    "github": "гитхаб", "python": "пайтон", "linux": "линукс", "zoom": "зум",
    "skype": "скайп", "word": "ворд", "excel": "эксель", "powerpoint": "пауэрпойнт",
    "outlook": "аутлук", "minecraft": "майнкрафт", "twitch": "твич", "reddit": "реддит",
    "wikipedia": "википедия", "bing": "бинг", "duckduckgo": "дакдакго", "gmail": "джимейл",
    "linkedin": "линкедин", "pinterest": "пинтерест", "viber": "вайбер", "slack": "слэк",
    "notion": "ноушен", "steam's": "стима", "vpn": "ви-пи-эн", "luna": "луна",
    "app": "эп", "web": "веб", "wiki": "вики", "pro": "про", "max": "макс", "plus": "плюс",
    "hello": "хеллоу", "hi": "хай", "bye": "бай", "sms": "эс-эм-эс", "ai": "эй-ай",
}  # fmt: skip
_LETTERS = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч",
    "i": "ай", "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн", "o": "оу", "p": "пи",
    "q": "кью", "r": "ар", "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл-ю",
    "x": "экс", "y": "уай", "z": "зед",
}  # fmt: skip
# Longest first; applied left to right.
_TRANSLIT = [
    ("tch", "ч"), ("sch", "ск"), ("tion", "шн"), ("igh", "ай"), ("dge", "дж"),
    ("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("ck", "к"), ("kh", "х"),
    ("zh", "ж"), ("ts", "ц"), ("qu", "кв"), ("oo", "у"), ("ee", "и"), ("ea", "и"),
    ("ou", "ау"), ("ow", "оу"), ("ai", "эй"), ("ay", "эй"), ("oy", "ой"), ("ng", "нг"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"),
    ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"),
    ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
    ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "и"), ("z", "з"),
]  # fmt: skip
_LATIN_RE = re.compile(r"[A-Za-z]+(?:[-'’][A-Za-z]+)*")
_SOFT_C = re.compile(r"c(?=[eiy])")
MAX_SPELLED_CAPS = 5


def _transliterate(word: str) -> str:
    word = _SOFT_C.sub("s", word)
    out: list[str] = []
    i = 0
    while i < len(word):
        for latin, cyr in _TRANSLIT:
            if word.startswith(latin, i):
                out.append(cyr)
                i += len(latin)
                break
        else:  # unreachable for ASCII letters; keep the character
            out.append(word[i])
            i += 1
    return "".join(out)


def _latin_sub(match: re.Match[str]) -> str:
    word = match.group(0)
    key = word.lower().replace("’", "'")
    if key in _BRANDS:
        return _BRANDS[key]
    if "-" in key or "'" in key:
        parts = re.split(r"[-']", key)
        return "-".join(_BRANDS.get(p) or _transliterate(p) for p in parts if p)
    if word.isupper() and 2 <= len(word) <= MAX_SPELLED_CAPS:
        return "-".join(_LETTERS[c] for c in key)
    return _transliterate(key)


_SYMBOLS_EN = {"₽": "rubles", "$": "dollars", "€": "euros", "%": "percent"}
_EN_SYMBOL_RE = re.compile(
    rf"(?:(?P<pre>[$€₽])\s?(?P<n1>{_NUM}))|(?:(?P<n2>{_NUM})\s?(?P<post>[$€₽%]))"
)


def _en_sub(match: re.Match[str]) -> str:
    if match.group("pre"):
        return f"{match.group('n1')} {_SYMBOLS_EN[match.group('pre')]}"
    return f"{match.group('n2')} {_SYMBOLS_EN[match.group('post')]}"


def normalize_for_speech(text: str, lang: str) -> str:
    """Rewrite ``text`` so a local RU/EN voice pronounces numbers, symbols and brands well."""
    lang = lang.strip().lower()
    if lang == "ru":
        text = _YEAR_RE.sub(_year_sub, text)
        text = _TIME_RE.sub(_time_sub, text)
        text = _NUMBER_RE.sub(_number_sub, text)
        text = _LATIN_RE.sub(_latin_sub, text)
    elif lang == "en":
        text = _EN_SYMBOL_RE.sub(_en_sub, text)
    return re.sub(r" {2,}", " ", text).strip()
