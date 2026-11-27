"""Данные для страниц «Сделки» и карточки: из БД в плоские структуры для шаблонов. Без запросов к сети."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.agent.schemas import Analysis
from app.db.models import (
    AgentStep,
    CatalogItem,
    Deal,
    DealFile,
    DealStatus,
    Feedback,
    Finding,
    MatchKind,
    Platform,
    Proposal,
)
from app.notify.letters import rub, short_rub
from app.pipeline.prefilter import PROCEDURE_RU
from app.report.excel import CONTRACT_RU, MSK, PAYMENT_RU, STATUS_RU, TRI_RU

FILES_ROOT = Path("/app/data/files")
WORKED = (DealStatus.HOT, DealStatus.ANALYZED, DealStatus.TAKEN, DealStatus.SUBMITTED, DealStatus.SKIPPED)
MANAGER_ACTIONS = (DealStatus.TAKEN, DealStatus.SUBMITTED, DealStatus.SKIPPED)
ACTION_RU = {DealStatus.TAKEN: "Беру", DealStatus.SUBMITTED: "Подано", DealStatus.SKIPPED: "Не беру"}


class View(StrEnum):
    OPEN = "open"
    TODAY = "today"
    ALL = "all"
    EXCLUDED = "excluded"


VIEW_RU = {
    View.OPEN: "Идёт приём КП",
    View.TODAY: "Найдены сегодня",
    View.ALL: "Все разобранные",
    View.EXCLUDED: "Отсеянные",
}


class Verdict(StrEnum):
    TAKE = "take"
    DECIDE = "decide"
    SKIP = "skip"
    WAIT = "wait"


VERDICT_RU = {
    Verdict.TAKE: "Стоит взять",
    Verdict.DECIDE: "Нужно ваше решение",
    Verdict.SKIP: "Не берём",
    Verdict.WAIT: "Ещё разбираем",
}


def verdict(deal: Deal) -> Verdict:
    if deal.status is DealStatus.FILTERED_OUT:
        return Verdict.SKIP
    if deal.conditions_met is None:
        return Verdict.WAIT
    if deal.status is DealStatus.HOT or deal.conditions_met == 5:
        return Verdict.TAKE
    return Verdict.DECIDE if deal.conditions_met >= 4 else Verdict.SKIP


def time_left(end_at: datetime | None, now: datetime) -> str:
    if end_at is None:
        return "срок не указан"
    delta = end_at - now
    if delta <= timedelta(0):
        return "приём закрыт"
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"осталось {days} дн. {hours} ч"
    if hours:
        return f"осталось {hours} ч {mins} мин"
    return f"осталось {mins} мин"


def urgent(end_at: datetime | None, now: datetime) -> bool:
    return end_at is not None and now < end_at <= now + timedelta(hours=24)


def msk(moment: datetime | None) -> str:
    return "не указано" if moment is None else moment.astimezone(MSK).strftime("%d.%m %H:%M")


def view_query(view: View, now: datetime) -> Select[Deal]:
    day_start = now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(Deal)
    if view is View.EXCLUDED:
        return q.where(Deal.status == DealStatus.FILTERED_OUT, or_(Deal.end_at > now, Deal.end_at.is_(None)))
    q = q.where(Deal.status.in_(WORKED))
    if view is View.OPEN:
        q = q.where(Deal.end_at > now)
    elif view is View.TODAY:
        q = q.where(Deal.created_at >= day_start)
    return q


@dataclass(frozen=True)
class DealRow:
    id: int
    title: str
    subject: str
    customer: str
    platform: str
    procedure: str
    amount: str
    n: int | None
    verdict: Verdict
    verdict_ru: str
    end_msk: str
    left: str
    urgent: bool
    status: DealStatus
    status_ru: str
    has_kp: bool
    reason: str | None
    url: str


def _subject(deal: Deal) -> str:
    raw = (deal.score_breakdown.get("analysis") or {}).get("result") or {}
    subject = raw.get("subject") if isinstance(raw, dict) else None
    return subject if isinstance(subject, str) and subject else deal.title


def deal_rows(session: Session, view: View, now: datetime) -> list[DealRow]:
    deals = list(session.scalars(view_query(view, now)))
    deals.sort(
        key=lambda d: (
            -(d.conditions_met or 0),
            d.end_at or datetime.max.replace(tzinfo=now.tzinfo),
            -float(d.score or 0),
        )
    )
    platforms = dict(session.execute(select(Platform.id, Platform.name)).tuples().all())
    ids = [d.id for d in deals]
    with_kp = set(session.scalars(select(Proposal.deal_id).where(Proposal.deal_id.in_(ids))))
    rows = []
    for d in deals:
        v = verdict(d)
        rows.append(
            DealRow(
                id=d.id,
                title=d.title,
                subject=_subject(d),
                customer=d.customer_name or "заказчик не указан",
                platform=platforms.get(d.platform_id, "площадка"),
                procedure=PROCEDURE_RU[d.procedure],
                amount=short_rub(d.nmck),
                n=d.conditions_met,
                verdict=v,
                verdict_ru=VERDICT_RU[v],
                end_msk=msk(d.end_at),
                left=time_left(d.end_at, now),
                urgent=urgent(d.end_at, now),
                status=d.status,
                status_ru=STATUS_RU[d.status],
                has_kp=d.id in with_kp,
                reason=d.filter_reason,
                url=d.url,
            )
        )
    return rows


def view_counts(session: Session, now: datetime) -> dict[View, int]:
    return {
        v: session.scalar(select(func.count()).select_from(view_query(v, now).subquery())) or 0 for v in View
    }


@dataclass(frozen=True)
class PositionView:
    name: str
    qty: str
    quote: str
    match: str
    match_kind: MatchKind
    sku: str | None
    catalog_name: str | None
    catalog_url: str | None
    price: str | None
    reason: str
    confidence: int


@dataclass(frozen=True)
class FileView:
    id: str
    name: str
    size_kb: int | None
    local: bool
    source_url: str | None


@dataclass(frozen=True)
class StepView:
    at: str
    step: str
    model: str | None
    tokens: int
    cost: str
    ms: int
    note: str
    error: bool


@dataclass
class DealCard:
    row: DealRow
    url: str
    nmck: str
    catalog_total: str | None
    payment: str
    contract: str
    has_tz: str
    smb_only: str
    min_kp_days: int | None
    conditions: list[dict[str, Any]]
    why: list[str]
    check: str | None
    findings: list[str]
    missing: list[str]
    positions: list[PositionView]
    not_license_reason: str | None
    kp_files: list[FileView]
    kp_blockers: list[str]
    docs: list[FileView]
    steps: list[StepView]
    agent_cost: str
    agent_model: str | None
    priority: list[tuple[str, float, float]]
    score: float | None
    rules_version: str | None
    history: list[tuple[str, str, str | None]] = field(default_factory=list)


def _analysis(deal: Deal) -> Analysis | None:
    raw = (deal.score_breakdown.get("analysis") or {}).get("result")
    if raw is None:
        return None
    try:
        return Analysis.model_validate(raw)
    except ValidationError:
        return None


def _step_note(step: AgentStep) -> str:
    out = step.output or {}
    if step.step.startswith("tool:"):
        args = out.get("args") or {}
        return ", ".join(f"{k}: {v}" for k, v in args.items())[:160]
    calls = out.get("tool_calls")
    if isinstance(calls, list) and calls:
        return "вызвал " + ", ".join(str(c) for c in calls)
    return ""


COMPONENT_RU = {
    "coverage": "Покрытие каталогом",
    "margin": "Маржа",
    "amount": "Сумма",
    "payment": "Оплата",
    "simplicity": "Простота",
}


def deal_card(session: Session, deal: Deal, now: datetime) -> DealCard:
    platform = session.get(Platform, deal.platform_id)
    analysis = _analysis(deal)
    score: dict[str, Any] = deal.score_breakdown.get("score") or {}
    expl: dict[str, Any] = deal.score_breakdown.get("explanation") or {}
    agent: dict[str, Any] = deal.score_breakdown.get("analysis") or {}
    v = verdict(deal)

    items: dict[int, CatalogItem] = {}
    if analysis is not None:
        ids = [p.catalog_item_id for p in analysis.positions if p.catalog_item_id is not None]
        items = {i.id: i for i in session.scalars(select(CatalogItem).where(CatalogItem.id.in_(ids)))}
    positions = []
    for p in analysis.positions if analysis else []:
        item = items.get(p.catalog_item_id or -1)
        positions.append(
            PositionView(
                name=p.name,
                qty=f"{p.qty:g} {p.unit or ''}".strip() if p.qty else "кол-во не указано",
                quote=p.quote,
                match={
                    MatchKind.EXACT: "точно",
                    MatchKind.ANALOG: "аналог",
                    MatchKind.NONE: "нет в каталоге",
                }[p.match],
                match_kind=p.match,
                sku=item.sku if item else None,
                catalog_name=item.name if item else None,
                catalog_url=item.url if item else None,
                price=(rub(item.price) if item.price is not None else None) if item else None,
                reason=p.reason,
                confidence=round(p.confidence * 100),
            )
        )

    proposal = session.scalar(
        select(Proposal).where(Proposal.deal_id == deal.id).order_by(Proposal.version.desc()).limit(1)
    )
    kp_files = []
    if proposal is not None:
        for i, path in enumerate((proposal.file_path, (proposal.items or {}).get("pdf"))):
            if path and Path(path).is_file():
                kp_files.append(
                    FileView(f"kp{i}", Path(path).name, Path(path).stat().st_size // 1024, True, None)
                )

    saved = {f.name: f for f in session.scalars(select(DealFile).where(DealFile.deal_id == deal.id))}
    details = deal.raw_masked.get("details") or {}
    docs = [
        FileView(str(f.id), f.name, f.size // 1024, Path(f.path).is_file(), f.source_url)
        for f in saved.values()
    ]
    for f in details.get("files") or []:
        if isinstance(f, dict) and f.get("name") and f["name"] not in saved:
            docs.append(FileView("", str(f["name"]), None, False, None))

    raw_steps = list(
        session.scalars(select(AgentStep).where(AgentStep.deal_id == deal.id).order_by(AgentStep.id))
    )
    steps = [
        StepView(
            at=s.created_at.astimezone(MSK).strftime("%d.%m %H:%M:%S"),
            step=s.step,
            model=s.model,
            tokens=s.tokens_in + s.tokens_out,
            cost=f"{s.cost_rub:.2f} ₽".replace(".", ","),
            ms=s.ms,
            note=_step_note(s),
            error=s.error is not None,
        )
        for s in raw_steps
    ]
    agent_cost = sum((s.cost_rub for s in raw_steps), Decimal(0))

    history = [
        (fb.created_at.astimezone(MSK).strftime("%d.%m %H:%M"), STATUS_RU[fb.action], fb.reason)
        for fb in session.scalars(
            select(Feedback).where(Feedback.deal_id == deal.id).order_by(Feedback.id.desc())
        )
    ]
    findings = list(
        dict.fromkeys(
            f.text
            for f in session.scalars(select(Finding).where(Finding.deal_id == deal.id).order_by(Finding.id))
        )
    )
    weights = score.get("weights") or {}
    catalog_total = score.get("catalog_total")
    pay = PAYMENT_RU[deal.payment_terms] + (f" {deal.payment_days} дн." if deal.payment_days else "")

    return DealCard(
        row=DealRow(
            id=deal.id,
            title=deal.title,
            subject=analysis.subject if analysis else deal.title,
            customer=deal.customer_name or "заказчик не указан",
            platform=platform.name if platform else "площадка",
            procedure=PROCEDURE_RU[deal.procedure],
            amount=short_rub(deal.nmck),
            n=deal.conditions_met,
            verdict=v,
            verdict_ru=VERDICT_RU[v],
            end_msk=msk(deal.end_at),
            left=time_left(deal.end_at, now),
            urgent=urgent(deal.end_at, now),
            status=deal.status,
            status_ru=STATUS_RU[deal.status],
            has_kp=bool(kp_files),
            reason=deal.filter_reason,
            url=deal.url,
        ),
        url=deal.url,
        nmck=rub(deal.nmck),
        catalog_total=rub(Decimal(catalog_total)) if catalog_total else None,
        payment=pay,
        contract=CONTRACT_RU[deal.contract_format],
        has_tz=TRI_RU[deal.has_tz],
        smb_only=TRI_RU[deal.smb_only],
        min_kp_days=deal.kp_min_valid_days,
        conditions=list(score.get("conditions") or []),
        why=list(expl.get("why") or []),
        check=expl.get("check"),
        findings=findings,
        missing=[m for m in (analysis.missing if analysis else []) if m not in findings],
        positions=positions,
        not_license_reason=analysis.not_license_reason
        if analysis and not analysis.is_license_supply
        else None,
        kp_files=kp_files,
        kp_blockers=list(deal.score_breakdown.get("kp_blockers") or []),
        docs=docs,
        steps=steps,
        agent_cost=f"{agent_cost:.2f} ₽".replace(".", ","),
        agent_model=agent.get("model"),
        priority=[
            (COMPONENT_RU.get(k, k), float(val), float(weights.get(k, 0)))
            for k, val in (score.get("components") or {}).items()
        ],
        score=score.get("score"),
        rules_version=deal.rules_version,
        history=history,
    )


def file_path(session: Session, deal: Deal, file_id: str) -> Path | None:
    """Путь к файлу сделки по id из карточки; только внутри FILES_ROOT, иначе None."""
    path: str | None = None
    if file_id in ("kp0", "kp1"):
        proposal = session.scalar(
            select(Proposal).where(Proposal.deal_id == deal.id).order_by(Proposal.version.desc()).limit(1)
        )
        if proposal is not None:
            path = proposal.file_path if file_id == "kp0" else (proposal.items or {}).get("pdf")
    elif file_id.isdigit():
        row = session.get(DealFile, int(file_id))
        if row is not None and row.deal_id == deal.id:
            path = row.path
    if not path:
        return None
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(FILES_ROOT.resolve()) or not resolved.is_file():
        return None
    return resolved


def find_by_url(session: Session, url: str) -> Deal | None:
    """Сделка по адресу вкладки: без якоря и хвостового «/», с www и без."""
    clean = url.split("#", 1)[0].rstrip("/")
    variants = {clean, clean.replace("://www.", "://", 1), clean.replace("://", "://www.", 1)}
    return session.scalars(
        select(Deal).where(Deal.url.in_(variants)).order_by(Deal.id.desc()).limit(1)
    ).first()
