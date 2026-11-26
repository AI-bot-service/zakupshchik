"""Объяснение для менеджера (S8): дешёвая модель пишет по структуре, код сверяет числа.

Не прошло проверку — текст из условий.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.analyze import Prompt, load_prompt
from app.agent.journal import Journal
from app.agent.llm import LLM
from app.db.models import FindingKind
from app.scoring.score import ScoreResult

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_JSON = re.compile(r"\{.*\}", re.DOTALL)


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    why: list[str] = Field(min_length=1, max_length=3)
    check: str = Field(min_length=3, max_length=200)
    by_model: bool = False


def _digits(text: str) -> set[str]:
    """Числа без разделителей разрядов: «183 600» и «183600» — одно число."""
    flat = re.sub(r"(?<=\d)[\s ](?=\d{3}\b)", "", text)
    return {f"{Decimal(n.replace(',', '.')).normalize():f}" for n in _NUMBER.findall(flat)}


def fallback(result: ScoreResult) -> Explanation:
    met = [c.reason for c in result.conditions if c.met]
    unmet = [c.reason for c in result.conditions if not c.met]
    why = ([f"{result.n} из 5: " + "; ".join(met[:2])] if met else []) + [
        f"Не выполнено: {r}" for r in unmet[:2]
    ]
    risks = [f.text for f in result.findings if f.kind is not FindingKind.CONFLICT]
    check = risks[0] if risks else (unmet[0] if unmet else "цены и наличие лицензий перед отправкой КП")
    return Explanation(why=[w[:140] for w in why[:3]] or ["Разбор без замечаний"], check=check[:140])


def check_numbers(expl: Explanation, source: str) -> list[str]:
    allowed = _digits(source)
    texts = [*expl.why, expl.check]
    bad = sorted({n for t in texts for n in _digits(t)} - allowed)
    errors = [f"числа {', '.join(bad)} нет во входных данных"] if bad else []
    errors += [f"пункт длиннее 140 символов: «{t[:40]}…»" for t in texts if len(t) > 140]
    return errors


def explain(
    llm: LLM,
    model: str,
    facts: dict[str, Any],
    result: ScoreResult,
    journal: Journal,
    prompt: Prompt | None = None,
) -> Explanation:
    prompt = prompt or load_prompt("explain")
    source = json.dumps(facts, ensure_ascii=False, default=str)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": f"<разбор>\n{source}\n</разбор>"},
    ]
    for attempt in (1, 2):
        try:
            reply = llm.chat(model, messages, max_tokens=3000)
        except Exception as exc:  # объяснение не критично: без него сделка всё равно оценена
            journal.add("explain", error={"code": "llm_failed", "message_ru": type(exc).__name__})
            break
        errors: list[str]
        expl: Explanation | None = None
        match = _JSON.search(reply.content or "")
        try:
            expl = Explanation.model_validate_json(match.group(0) if match else "")
            errors = check_numbers(expl, source)
        except ValidationError as exc:
            errors = [f"ответ не по формату: {exc.errors()[0]['msg']}"]
        journal.add(
            "explain",
            output={"attempt": attempt, "errors": errors, "prompt": prompt.version},
            model=reply.model,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
            cost_rub=reply.cost_rub,
            ms=reply.ms,
        )
        if expl is not None and not errors:
            return expl.model_copy(update={"by_model": True})
        messages += [
            {"role": "assistant", "content": reply.content or ""},
            {"role": "user", "content": "Исправь: " + "; ".join(errors) + ". Верни только JSON."},
        ]
    return fallback(result)
