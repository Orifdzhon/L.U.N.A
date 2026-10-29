import ast
import re
from pathlib import Path

import pytest

from luna.core import prompts

SRC = Path(__file__).resolve().parents[3] / "src" / "luna"
MASCULINE = re.compile(
    r"(?<!\w)(?:понял|открыл|нашёл|нашел|сделал|закрыл|готов|запустил|выполнил)(?!\w)",
    re.IGNORECASE,
)


def _prompts(lang: str) -> list[str]:
    return [
        prompts.build_system_prompt(lang, "now"),
        prompts.build_system_prompt(lang, "now", force_search=True),
        prompts.build_answer_prompt(lang, "now"),
    ]


@pytest.mark.parametrize("prompt", _prompts("ru"))
def test_ru_prompts_are_feminine_with_capabilities(prompt: str) -> None:
    assert "только в женском роде" in prompt
    assert "поняла, открыла, нашла" in prompt
    assert "что умеешь" in prompt
    assert "без списков" in prompt
    assert "Orif" in prompt


@pytest.mark.parametrize("prompt", _prompts("en"))
def test_en_prompts_are_feminine_with_capabilities(prompt: str) -> None:
    assert "she/her" in prompt
    assert "what you can do" in prompt
    assert "Orif" in prompt


def test_fallbacks_have_no_masculine_forms() -> None:
    for texts in prompts.FALLBACKS.values():
        assert not MASCULINE.search(texts["ru"])


def test_no_fixed_russian_string_uses_masculine_self_forms() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            is_text = isinstance(node, ast.Constant) and isinstance(node.value, str)
            if is_text and MASCULINE.search(node.value):
                offenders.append((path.name, node.value[:60]))
    assert not offenders


def test_find_file_instruction_requires_listing_all_numbered_results():
    en = prompts.build_system_prompt("en", "now")
    assert "list ALL returned results, numbered 1..N" in en
    assert "'and N more'" in en
    ru = prompts.build_system_prompt("ru", "now")
    assert "перечисли ВСЕ возвращённые результаты с номерами 1..N" in ru  # noqa: RUF001
    assert "«и ещё N»" in ru
