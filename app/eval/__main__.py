"""Проверка качества на размеченных закупках (ARCHITECTURE.md §13): отбор, сопоставление, ₽ и время.

Запуск на стенде: docker exec zakupshchik-infra-app python -m app.eval [--rules config/rules.yaml]
Без --rules берутся действующие правила из БД. Префильтр пересчитывается на момент, когда робот впервые увидел
закупку (иначе всё «истекло»); решения агента и стоимость берутся из журнала шагов как есть.
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.catalog.vendors import load_vendors
from app.collector import VENDORS_FILE
from app.core.settings import Settings
from app.db.models import AgentStep, Deal, Platform
from app.db.session import make_engine, make_session_factory
from app.pipeline.prefilter import DealFacts, check
from app.scoring.rules import Rules, load_rules
from app.scoring.store import active_rules

LABELS_FILE = Path(__file__).with_name("labels.yaml")
MatchLabel = Literal["exact", "analog", "none"]
_RANK: dict[str, int] = {"none": 0, "analog": 1, "exact": 2}


class Label(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ours: bool | None
    why: str
    match: MatchLabel | None = None


def load_labels(path: Path = LABELS_FILE) -> dict[str, Label]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(k): Label.model_validate(v) for k, v in data.items()}


@dataclass
class Row:
    key: str
    title: str
    label: Label
    passed: bool
    cut: str
    # Результат агента, если разбирали: лучшая найденная позиция и выполнено ли условие «ПО из каталога»
    agent_match: str | None
    catalog_met: bool | None
    cost_rub: Decimal
    seconds: float


def _agent(breakdown: dict[str, Any]) -> tuple[str | None, bool | None]:
    positions = breakdown.get("analysis", {}).get("result", {}).get("positions")
    if not positions:
        return None, None
    best = max((str(p.get("match", "none")) for p in positions), key=lambda m: _RANK.get(m, 0))
    conditions = breakdown.get("score", {}).get("conditions", [])
    met = next(
        (bool(c["met"]) for c in conditions if c.get("key") == "catalog"),
        None,
    )
    return best, met


def collect(session: Session, rules: Rules, labels: dict[str, Label]) -> list[Row]:
    vendors = load_vendors(VENDORS_FILE)
    costs = {
        deal_id: (cost, ms)
        for deal_id, cost, ms in session.execute(
            select(AgentStep.deal_id, func.sum(AgentStep.cost_rub), func.sum(AgentStep.ms))
            .where(AgentStep.model.is_not(None))
            .group_by(AgentStep.deal_id)
        )
    }
    deals = session.execute(
        select(Deal, Platform.code).join(Platform).options(selectinload(Deal.items))
    ).all()
    rows: list[Row] = []
    for deal, code in deals:
        key = f"{code}:{deal.external_id}"
        label = labels.get(key)
        if label is None:
            continue
        verdict = check(
            DealFacts(
                deal.title,
                deal.nmck,
                deal.procedure,
                deal.law,
                deal.end_at,
                deal.smb_only,
                tuple(i.name for i in deal.items),
            ),
            rules,
            vendors,
            deal.created_at,
        )
        match, met = _agent(deal.score_breakdown)
        cost, ms = costs.get(deal.id, (Decimal(0), 0))
        rows.append(
            Row(key, deal.title, label, verdict.passed, "; ".join(verdict.cut), match, met, cost, ms / 1000)
        )
    return rows


def _pct(num: int, den: int) -> str:
    return f"{num / den:.0%} ({num}/{den})" if den else "—"


def _short(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def report(rows: list[Row], rules: Rules) -> str:
    labeled = [r for r in rows if r.label.ours is not None]
    ours = [r for r in labeled if r.label.ours]
    passed = [r for r in labeled if r.passed]
    theirs = [r for r in labeled if not r.label.ours]
    tp = [r for r in ours if r.passed]

    matched = [r for r in rows if r.label.match and r.agent_match]
    match_ok = [r for r in matched if r.agent_match == r.label.match]
    # «Взял бы»: условие «ПО из каталога» выполнено агентом
    analyzed_ours = [r for r in ours if r.catalog_met is not None]
    taken = [r for r in analyzed_ours if r.catalog_met]
    recommended = [r for r in labeled if r.passed and r.catalog_met]
    rec_ours = [r for r in recommended if r.label.ours]

    analyzed = [r for r in rows if r.cost_rub > 0]
    rub = [float(r.cost_rub) for r in analyzed]
    sec = [r.seconds for r in analyzed]

    out = [
        f"Правила `{rules.version}`, размечено {len(rows)} закупок, "
        f"из них спорных {len(rows) - len(labeled)} "
        f"(в метрики не входят), наших {len(ours)}.",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        f"| Префильтр: полнота (наши, которые прошли) | {_pct(len(tp), len(ours))} |",
        f"| Префильтр: точность (прошедшие, которые наши) | {_pct(len(tp), len(passed))} |",
        f"| Префильтр: верно отсеяно не наших | {_pct(len(theirs) - (len(passed) - len(tp)), len(theirs))} |",
        f"| Агент: позиция каталога совпала с разметкой | {_pct(len(match_ok), len(matched))} |",
        f"| Агент: наша закупка признана «ПО из каталога» | {_pct(len(taken), len(analyzed_ours))} |",
        f"| Итог: рекомендованные роботом закупки, которые наши | {_pct(len(rec_ours), len(recommended))} |",
    ]
    if analyzed:
        out += [
            f"| ₽ на разобранную закупку: среднее / медиана / максимум | {statistics.mean(rub):.2f} / "
            f"{statistics.median(rub):.2f} / {max(rub):.2f} ({len(analyzed)} закупок) |",
            f"| Секунд модели на закупку: среднее / максимум | {statistics.mean(sec):.0f} / {max(sec):.0f} |",
        ]

    errors: list[tuple[str, str, str, str]] = []
    for r in labeled:
        if r.label.ours and not r.passed:
            errors.append((r.key, _short(r.title), "наша, отсеяна префильтром", r.cut))
        elif not r.label.ours and r.passed and r.catalog_met is not False:
            # Прошла префильтр, но агент сказал «нет в каталоге» — так задумано: вендор в заголовке не назван
            errors.append((r.key, _short(r.title), "не наша, прошла префильтр", r.label.why))
        elif r.label.ours and r.passed and r.catalog_met is None:
            errors.append((r.key, _short(r.title), "наша, агент не разбирал", "прошла бы при этих правилах"))
    for r in matched:
        if r.agent_match != r.label.match:
            errors.append(
                (r.key, _short(r.title), f"агент: {r.agent_match}, ждали {r.label.match}", r.label.why)
            )
    out += ["", "**Ошибки**", ""]
    if errors:
        out += ["| Закупка | Название | Ошибка | Причина / разметка |", "|---|---|---|---|"]
        out += [f"| {k} | {t} | {e} | {w} |" for k, t, e, w in errors]
    else:
        out.append("Нет.")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", type=Path, help="файл правил вместо действующих в БД")
    args = parser.parse_args()
    factory = make_session_factory(make_engine(Settings().database_url.get_secret_value().encoded_string()))
    with factory() as session:
        rules = load_rules(args.rules) if args.rules else active_rules(session)
        print(report(collect(session, rules, load_labels()), rules))
        session.rollback()


if __name__ == "__main__":
    main()
