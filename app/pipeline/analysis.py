"""Шаг «разбор»: сделки, прошедшие префильтр → агент → ограничения и «N из 6» → объяснение → БД.

Идемпотентно: сделка с тем же входом (карточка + модель + версия промпта) второй раз в LLM не идёт.
Пересчёт оценки (время до подачи, правка rules.yaml) — каждый цикл по сохранённому разбору, без токенов.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.config import PlatformConfig
from app.adapters.fetch import Fetcher
from app.agent.analyze import (
    AgentContext,
    AgentResult,
    DbCatalog,
    analyze_deal,
    input_hash,
    load_prompt,
    platform_attachments,
)
from app.agent.explain import Explanation, explain, fallback
from app.agent.journal import Journal
from app.agent.llm import LLM
from app.agent.schemas import Analysis
from app.catalog.vendors import VendorIndex
from app.db.models import CatalogItem, Deal, DealStatus, Finding, Platform, TriState
from app.pipeline.status_log import Who, change_status, robot
from app.scoring.rules import Rules
from app.scoring.score import CONDITIONS_TOTAL, DealView, ScoreResult, evaluate

log = structlog.get_logger("analysis")
ANALYZED = (DealStatus.ANALYZED, DealStatus.HOT)


@dataclass(frozen=True)
class Models:
    main: str
    cheap: str


@dataclass
class AnalysisRun:
    analyzed: int = 0
    hot: int = 0
    needs_human: int = 0
    skipped_same_input: int = 0
    cost_rub: Decimal = Decimal(0)
    errors: list[str] = field(default_factory=list)


def deal_view(deal: Deal) -> DealView:
    details = deal.raw_masked.get("details", {})
    files = details.get("files") or []
    return DealView(
        title=deal.title,
        nmck=deal.nmck,
        procedure=deal.procedure,
        end_at=deal.end_at,
        smb_only=deal.smb_only,
        file_names=tuple(str(f.get("name", "")) for f in files if isinstance(f, dict)),
        guarantee_flag=details.get("contract_guarantee_required") is True,
    )


def catalog_items(session: Session, analysis: Analysis) -> dict[int, CatalogItem]:
    ids = {p.catalog_item_id for p in analysis.positions if p.catalog_item_id is not None}
    if not ids:
        return {}
    return {i.id: i for i in session.scalars(select(CatalogItem).where(CatalogItem.id.in_(ids)))}


def explain_facts(
    deal: Deal, analysis: Analysis, result: ScoreResult, items: dict[int, CatalogItem]
) -> dict[str, Any]:
    return {
        "закупка": deal.title[:300],
        "заказчик": deal.customer_name,
        "НМЦ_руб": str(deal.nmck) if deal.nmck is not None else None,
        "что_покупают": analysis.subject,
        "условий_выполнено": f"{result.n} из 6",
        "условия": [{"условие": c.title, "выполнено": c.met, "почему": c.reason} for c in result.conditions],
        "риски": [f.text for f in result.findings],
        "товары_каталога": [
            {
                "позиция": p.name,
                "товар": items[p.catalog_item_id].name,
                "кол-во": p.qty,
                "цена_руб": str(items[p.catalog_item_id].price)
                if items[p.catalog_item_id].price is not None
                else None,
            }
            for p in analysis.positions
            if p.catalog_item_id in items
        ],
        "сумма_по_каталогу_руб": str(result.catalog_total) if result.catalog_total is not None else None,
    }


WHO_AGENT = robot("разбор агентом")
WHO_SCORE = robot("пересчёт по правилам")


def _apply_score(
    session: Session, deal: Deal, analysis: Analysis, result: ScoreResult, rules: Rules, who: Who
) -> None:
    deal.conditions_met = result.n
    deal.score = Decimal(str(result.score))
    deal.rules_version = rules.version
    to = DealStatus.HOT if result.hot else DealStatus.ANALYZED
    change_status(session, deal, to, who, f"{result.n} из {CONDITIONS_TOTAL} условий")
    deal.payment_terms = analysis.payment_terms
    deal.payment_days = analysis.payment_days
    deal.contract_format = analysis.contract_format
    deal.has_tz = TriState.YES if result.has_tz else TriState.NO
    session.execute(delete(Finding).where(Finding.deal_id == deal.id))
    for f in result.findings:
        session.add(Finding(deal_id=deal.id, kind=f.kind, text=f.text, quote=f.quote))
    deal.score_breakdown = {
        **deal.score_breakdown,
        "score": {
            "rules_version": rules.version,
            "n": result.n,
            "hot": result.hot,
            "conditions": [c.model_dump() for c in result.conditions],
            "components": result.components,
            "weights": rules.profile.weights.model_dump(),
            "score": result.score,
            "catalog_total": str(result.catalog_total) if result.catalog_total is not None else None,
            "positions_without_price": result.positions_without_price,
        },
    }


def _save_agent(deal: Deal, res: AgentResult, model: str) -> None:
    deal.score_breakdown = {
        **deal.score_breakdown,
        "analysis": {
            "input_hash": res.input_hash,
            "prompt_version": res.prompt_version,
            "model": model,
            "needs_human": res.needs_human,
            "result": res.analysis.model_dump(mode="json") if res.analysis else None,
            "files_read": res.files_read,
            "searches": res.searches,
            "turns": res.turns,
            "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out,
            "cost_rub": str(res.cost_rub),
        },
    }


def _candidates(session: Session, rules: Rules, with_details: set[str], limit: int) -> list[Deal]:
    codes = dict(session.execute(select(Platform.id, Platform.code)).tuples().all())
    deals = session.scalars(
        select(Deal)
        .where(Deal.status == DealStatus.NEW, Deal.rules_version == rules.version)
        .order_by(Deal.end_at.asc().nulls_last())
    ).all()
    ready = [d for d in deals if codes[d.platform_id] not in with_details or "details" in d.raw_masked]
    return ready[: limit * 3]


def run_analysis(
    factory: sessionmaker[Session],
    fetcher: Fetcher | None,
    configs: list[PlatformConfig],
    vendors: VendorIndex,
    rules: Rules,
    llm: LLM,
    models: Models,
    now: datetime,
    limit: int = 5,
) -> AnalysisRun:
    run = AnalysisRun()
    run_id = str(uuid.uuid4())
    prompt = load_prompt("analyze")
    by_code = {c.code: c for c in configs}
    with_details = {c.code for c in configs if c.details is not None}
    with factory() as session:
        ids = [d.id for d in _candidates(session, rules, with_details, limit)]
    for deal_id in ids:
        if run.analyzed + run.needs_human >= limit:
            break
        with factory.begin() as session:
            deal = session.get_one(Deal, deal_id)
            previous = deal.score_breakdown.get("analysis", {})
            if previous.get("input_hash") == input_hash(deal, prompt, models.main):
                run.skipped_same_input += 1
                continue
            code = session.get_one(Platform, deal.platform_id).code
            journal = Journal(factory, run_id, deal.id)
            ctx = AgentContext(
                catalog=DbCatalog(session, vendors),
                attachments=platform_attachments(session, fetcher, by_code.get(code)),
                llm=llm,
                model=models.main,
                journal=journal,
                catalog_vendors=tuple(
                    f"{v.name} ({', '.join(v.products)})" if v.products else v.name
                    for v in vendors.vendors.values()
                ),
            )
            try:
                res = analyze_deal(ctx, deal, prompt)
            except Exception as exc:  # сбой шлюза на одной сделке не останавливает остальные
                log.exception("agent_failed", deal_id=deal.id)
                journal.add("agent", error={"code": "agent_failed", "message_ru": type(exc).__name__})
                run.errors.append(f"{deal.id}: {type(exc).__name__}")
                if len(run.errors) >= 2:  # шлюз лежит или кончились деньги — не гоняем остальные сделки
                    break
                continue
            run.cost_rub += res.cost_rub
            _save_agent(deal, res, models.main)
            if res.analysis is None:
                deal.conditions_met = None
                change_status(
                    session, deal, DealStatus.ANALYZED, WHO_AGENT, "нужен человек: агент не разобрал"
                )
                run.needs_human += 1
                journal.add("agent", output={"needs_human": res.needs_human})
                continue
            items = catalog_items(session, res.analysis)
            result = evaluate(deal_view(deal), res.analysis, items, rules, now)
            _apply_score(session, deal, res.analysis, result, rules, WHO_AGENT)
            expl = explain(
                llm, models.cheap, explain_facts(deal, res.analysis, result, items), result, journal
            )
            deal.score_breakdown = {
                **deal.score_breakdown,
                "explanation": {**expl.model_dump(), "n": result.n},
            }
            journal.add("score", output={"n": result.n, "hot": result.hot, "score": result.score})
            run.analyzed += 1
            run.hot += result.hot
    return run


def rescore(session: Session, rules: Rules, now: datetime) -> dict[str, int]:
    """Разобранные сделки: истёкшие → EXPIRED, остальные пересчитать по текущим правилам и времени."""
    stats = {"rescored": 0, "changed": 0, "expired": 0}
    deals = session.scalars(select(Deal).where(Deal.status.in_(ANALYZED))).all()
    for deal in deals:
        if deal.end_at is not None and deal.end_at <= now:
            change_status(session, deal, DealStatus.EXPIRED, WHO_SCORE, "приём КП закончился")
            stats["expired"] += 1
            continue
        raw = (deal.score_breakdown.get("analysis") or {}).get("result")
        if raw is None:
            continue
        analysis = Analysis.model_validate(raw)
        before = (deal.conditions_met, deal.status)
        items = catalog_items(session, analysis)
        result = evaluate(deal_view(deal), analysis, items, rules, now)
        _apply_score(session, deal, analysis, result, rules, WHO_SCORE)
        stats["rescored"] += 1
        if (deal.conditions_met, deal.status) != before:
            stats["changed"] += 1
            # Текст модели писался под прежний итог: до нового текста модели — текст из условий, «устарело»
            expl: Explanation = fallback(result)
            deal.score_breakdown = {
                **deal.score_breakdown,
                "explanation": {**expl.model_dump(), "n": result.n, "stale": True},
            }
    return stats


def refresh_explanations(
    factory: sessionmaker[Session], llm: LLM, model: str, rules: Rules, now: datetime, limit: int = 10
) -> tuple[int, Decimal]:
    """Новый текст «почему» дешёвой моделью там, где правила изменили итог. Только открытые сделки."""
    run_id = str(uuid.uuid4())
    prompt = load_prompt("explain")
    with factory() as session:
        ids = [
            d.id
            for d in session.scalars(select(Deal).where(Deal.status.in_(ANALYZED), Deal.end_at > now))
            if (d.score_breakdown.get("explanation") or {}).get("stale")
        ][:limit]
    done, cost = 0, Decimal(0)
    for deal_id in ids:
        with factory.begin() as session:
            deal = session.get_one(Deal, deal_id)
            raw = (deal.score_breakdown.get("analysis") or {}).get("result")
            if raw is None:
                continue
            analysis = Analysis.model_validate(raw)
            items = catalog_items(session, analysis)
            result = evaluate(deal_view(deal), analysis, items, rules, now)
            journal = Journal(factory, run_id, deal.id)
            expl = explain(llm, model, explain_facts(deal, analysis, result, items), result, journal, prompt)
            deal.score_breakdown = {
                **deal.score_breakdown,
                "explanation": {**expl.model_dump(), "n": result.n},
            }
            done += 1
            cost += sum((Decimal(str(st["cost_rub"])) for st in journal.steps), Decimal(0))
    return done, cost
