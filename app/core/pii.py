"""Маскирование персональных данных до сохранения, LLM и журнала (ARCHITECTURE.md §10, §19).

Реквизиты организаций (название, ИНН юрлица) — не ПДн и остаются. Телефоны, почта, ФИО контактных лиц,
ИНН физлица (12 цифр) и СНИЛС заменяются метками.
"""

from __future__ import annotations

import re
from typing import Any

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[почта]"),
    (re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), "[телефон]"),
    (re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]\d{2}(?!\d)"), "[СНИЛС]"),
    (re.compile(r"(?<!\d)\d{12}(?!\d)"), "[ИНН ФЛ]"),
    # Фамилия И.О. / И.О. Фамилия
    (re.compile(r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.(?!\w)"), "[ФИО]"),
    (re.compile(r"(?<!\w)[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+\b"), "[ФИО]"),
    # Полное ФИО после «ИП», «контактное лицо», «ответственный» и т.п.
    (
        re.compile(
            r"((?:\bИП|[Ии]ндивидуальн\w*\s+предпринимател\w*|[Кк]онтактн\w*\s+лиц\w*"
            r"|[Оо]тветственн\w*(?:\s+лиц\w*)?|[Ии]сполнител\w*)\s*[:\-—]?\s*)"
            r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?"
        ),
        r"\1[ФИО]",
    ),
]


def mask_text(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


def mask(value: Any) -> Any:
    """Рекурсивно по строкам в dict/list; остальные типы без изменений."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value
