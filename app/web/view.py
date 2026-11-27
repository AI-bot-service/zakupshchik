"""Данные для страниц «Сделки» и карточки: из БД в плоские структуры для шаблонов. Без запросов к сети."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, Select, and_, func, or_, select, true
from sqlalchemy.orm import Session

from app.agent.schemas import Analysis
from app.db.models import (
    Actor,
    AgentStep,
    CatalogItem,
    Deal,
    DealFile,
    DealStatus,
    DealStatusLog,
    Finding,
    MatchKind,
    Platform,
    Proposal,
    ProposalStatus,
)
from app.notify.letter_html import SHORT, condition_text
from app.notify.letters import rub, short_rub
from app.pipeline.prefilter import PROCEDURE_RU
from app.pipeline.status_log import decided_at
from app.report.excel import CONTRACT_RU, PAYMENT_RU, TRI_RU, Period, worked_filter
from app.report.stamps import (
    MSK,
    STAMP_RU,
    STATUS_RU,
    VERDICT_RU,
    LogLine,
    Stamp,
    Verdict,
    log_line,
    msk,
    stamp,
    verdict,
)
from app.scoring.score import catalog_met

FILES_ROOT = Path("/app/data/files")
WORKED = (DealStatus.HOT, DealStatus.ANALYZED, DealStatus.TAKEN, DealStatus.SUBMITTED, DealStatus.SKIPPED)
MANAGER_ACTIONS = (DealStatus.TAKEN, DealStatus.SUBMITTED, DealStatus.SKIPPED)
ACTION_RU = {
    DealStatus.TAKEN: "В работу",
    DealStatus.SUBMITTED: "КП отправлено",
    DealStatus.SKIPPED: "Не беру",
}


class View(StrEnum):
    OPEN = "open"
    TODAY = "today"
    ALL = "all"
    EXCLUDED = "excluded"
    ARCHIVE = "archive"


VIEW_RU = {
    View.OPEN: "Активные",
    View.TODAY: "Найдены сегодня",
    View.ALL: "Все разобранные",
    View.EXCLUDED: "Отсеянные",
    View.ARCHIVE: "Архив",
}


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


def view_filter(view: View, now: datetime) -> ColumnElement[bool]:
    """Условие вкладки «Сделки»; им же фильтруется Excel, чтобы файл совпадал с экраном."""
    day_start = now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    if view is View.ARCHIVE:
        return true()  # вся история, любые статусы: истёкшие и отсеянные тоже
    if view is View.EXCLUDED:
        return and_(Deal.status == DealStatus.FILTERED_OUT, or_(Deal.end_at > now, Deal.end_at.is_(None)))
    if view is View.ALL:
        return worked_filter()
    worked = Deal.status.in_(WORKED)
    if view is View.OPEN:
        return and_(worked, Deal.end_at > now)
    return and_(worked, Deal.created_at >= day_start)  # TODAY


def view_query(view: View, now: datetime, period: Period | None = None) -> Select[Deal]:
    q = select(Deal).where(view_filter(view, now))
    return q.where(period.where(now)) if period else q


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
    catalog: bool
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
    found_msk: str
    published_msk: str
    stamp: Stamp
    stamp_ru: str
    stamp_sub: str


def _subject(deal: Deal) -> str:
    raw = (deal.score_breakdown.get("analysis") or {}).get("result") or {}
    subject = raw.get("subject") if isinstance(raw, dict) else None
    return subject if isinstance(subject, str) and subject else deal.title


def deal_rows(session: Session, view: View, now: datetime, period: Period | None = None) -> list[DealRow]:
    deals = list(session.scalars(view_query(view, now, period)))
    if view is View.ARCHIVE:
        deals.sort(key=lambda d: (d.start_at or d.created_at, d.id), reverse=True)
    else:
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
    decided = decided_at(session, ids)
    return [
        make_row(
            d, platforms.get(d.platform_id, "площадка"), _subject(d), d.id in with_kp, decided.get(d.id), now
        )
        for d in deals
    ]


def only_stamps(rows: list[DealRow], stamps: frozenset[Stamp]) -> list[DealRow]:
    """Фильтр по штампам: пусто — все строки."""
    return [r for r in rows if r.stamp in stamps] if stamps else rows


def stamp_counts(rows: list[DealRow]) -> dict[Stamp, int]:
    """Штампы, которые есть на вкладке, в порядке STAMP_RU — для чекбоксов фильтра."""
    found = {r.stamp for r in rows}
    return {s: sum(r.stamp is s for r in rows) for s in STAMP_RU if s in found}


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """1 запись, 2 записи, 5 записей."""
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def make_row(
    d: Deal, platform: str, subject: str, has_kp: bool, decided: datetime | None, now: datetime
) -> DealRow:
    v = verdict(d)
    st, sub = stamp(d, v, has_kp, decided, now)
    return DealRow(
        id=d.id,
        title=d.title,
        subject=subject,
        customer=d.customer_name or "заказчик не указан",
        platform=f"{platform} · демо-повтор" if "demo" in d.raw_masked else platform,
        procedure=PROCEDURE_RU[d.procedure],
        amount=short_rub(d.nmck),
        n=d.conditions_met,
        catalog=catalog_met(d.score_breakdown),
        verdict=v,
        verdict_ru=VERDICT_RU[v],
        end_msk=msk(d.end_at),
        left=time_left(d.end_at, now),
        urgent=urgent(d.end_at, now),
        status=d.status,
        status_ru=STATUS_RU[d.status],
        has_kp=has_kp,
        reason=d.filter_reason,
        url=d.url,
        found_msk=msk(d.created_at),
        published_msk=msk(d.start_at),
        stamp=st,
        stamp_ru=STAMP_RU[st],
        stamp_sub=sub,
    )


def view_counts(session: Session, now: datetime, period: Period | None = None) -> dict[View, int]:
    return {
        v: session.scalar(select(func.count()).select_from(view_query(v, now, period).subquery())) or 0
        for v in View
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
class KpInfo:
    """Последняя версия КП: чьё, под каким именем приложено, когда, ушло ли заказчику."""

    version: int
    by_manager: bool
    original: str | None
    made_msk: str
    sent: bool


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
    why_stale: bool
    findings: list[str]
    missing: list[str]
    positions: list[PositionView]
    not_license_reason: str | None
    kp_files: list[FileView]
    kp_by_manager: bool
    kp: KpInfo | None
    kp_blockers: list[str]
    docs: list[FileView]
    steps: list[StepView]
    agent_cost: str
    agent_model: str | None
    priority: list[tuple[str, float, float]]
    score: float | None
    rules_version: str | None
    history: list[LogLine] = field(default_factory=list)
    history_ru: str = ""
    steps_ru: str = ""
    track: list[TrackStep] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    can_skip: bool = False
    skip_reason: str = ""


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


@dataclass(frozen=True)
class TrackStep:
    """Шаг дорожки «Ход сделки»: done — пройден, now — сейчас, next — впереди, stop — сделка остановилась."""

    name: str
    when: str
    state: str


TRACK = ("Найдена", "Разобрана", "В работе", "КП отправлено")
STEP_OF = {
    DealStatus.NEW: 0,
    DealStatus.FILTERED_OUT: 1,
    DealStatus.ANALYZED: 1,
    DealStatus.HOT: 1,
    DealStatus.EXPIRED: 1,
    DealStatus.TAKEN: 2,
    DealStatus.SUBMITTED: 3,
}


def track(deal: Deal, log: list[DealStatusLog], now: datetime) -> list[TrackStep]:
    """Дорожка сделки по журналу статусов. Остановка (отсеяна, отказ, срок) заменяет оставшиеся шаги."""
    when = ["" for _ in TRACK]
    when[0] = msk(deal.created_at)
    for e in sorted(log, key=lambda e: e.id or 0):  # последняя запись о шаге — его дата
        i = STEP_OF.get(e.to_status)
        if i:
            when[i] = msk(e.at)
    closed = deal.end_at is not None and deal.end_at <= now
    st = deal.status
    stop: str | None = None
    if st is DealStatus.SKIPPED:
        before = next(
            (e.from_status for e in sorted(log, key=lambda e: -(e.id or 0)) if e.to_status is st), None
        )
        reached = STEP_OF.get(before, 1) if before is not None else 1
        stop = "Отказ"
    else:
        reached = STEP_OF[st]
        if st is DealStatus.FILTERED_OUT:
            reached, stop = 0, "Отсеяна"
        elif st is DealStatus.TAKEN and closed:
            stop = "Срок вышел"
        elif st is DealStatus.EXPIRED or (closed and st is not DealStatus.SUBMITTED):
            stop = "Приём закрыт"
    steps = [
        TrackStep(TRACK[i], when[i], "done" if i < reached or (i == reached and stop) else "now")
        for i in range(reached + 1)
    ]
    if stop:
        end = deal.end_at if stop in ("Приём закрыт", "Срок вышел") else None
        stop_log = next((e for e in sorted(log, key=lambda e: -(e.id or 0)) if e.to_status is st), None)
        stamp_when = msk(end) if end else (msk(stop_log.at) if stop_log else "")
        return [*steps, TrackStep(stop, stamp_when, "stop")]
    if st is DealStatus.SUBMITTED:
        return [*steps[:-1], TrackStep(TRACK[3], when[3], "done")]
    return steps + [TrackStep(TRACK[i], "", "next") for i in range(reached + 1, len(TRACK))]


@dataclass(frozen=True)
class Action:
    """Кнопка смены статуса в карточке: какой статус ставит, как называется и что произойдёт."""

    to: DealStatus
    label: str
    hint: str
    primary: bool = False


def next_actions(deal: Deal, has_kp: bool, now: datetime) -> list[Action]:
    """Что менеджер может сделать со сделкой сейчас. «Не беру» с причиной выводится отдельно (can_skip)."""
    closed = deal.end_at is not None and deal.end_at <= now
    analyzed = deal.conditions_met is not None
    match deal.status:
        case DealStatus.ANALYZED | DealStatus.HOT if not closed:
            return [
                Action(
                    DealStatus.TAKEN,
                    "Беру в работу",
                    "Робот соберёт КП по разбору: аналоги, позиции вне каталога и пустые цены тоже. "
                    "Напоминания о сделке прекратятся.",
                    primary=True,
                )
            ]
        case DealStatus.TAKEN:
            return [
                Action(
                    DealStatus.SUBMITTED,
                    "КП отправлено",
                    "Нажмите, когда отправите КП заказчику на площадке. На штампе будет «Отправлено» и дата."
                    if has_kp
                    else "",
                    primary=True,
                )
            ]
        case DealStatus.SUBMITTED:
            return [
                Action(
                    DealStatus.TAKEN, "Вернуть в работу", "Если отметили по ошибке. КП в карточке остаётся."
                )
            ]
        case DealStatus.SKIPPED if analyzed and not closed:
            return [
                Action(DealStatus.TAKEN, "Вернуть в работу", "Передумали — робот соберёт КП, как при «Беру».")
            ]
    return []


def can_skip(deal: Deal, now: datetime) -> bool:
    closed = deal.end_at is not None and deal.end_at <= now
    if deal.status is DealStatus.TAKEN:
        return True  # и после срока: сделку в работе закрывают отказом
    return not closed and deal.status in (
        DealStatus.NEW,
        DealStatus.ANALYZED,
        DealStatus.HOT,
        DealStatus.SKIPPED,
    )


def deal_card(session: Session, deal: Deal, now: datetime) -> DealCard:
    platform = session.get(Platform, deal.platform_id)
    analysis = _analysis(deal)
    score: dict[str, Any] = deal.score_breakdown.get("score") or {}
    expl: dict[str, Any] = deal.score_breakdown.get("explanation") or {}
    agent: dict[str, Any] = deal.score_breakdown.get("analysis") or {}
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
    kp: KpInfo | None = None
    if proposal is not None:
        meta = proposal.items or {}
        by_manager = meta.get("source") == "manager"
        original = meta.get("original_name") if by_manager else None
        kp = KpInfo(
            proposal.version,
            by_manager,
            original,
            msk(proposal.created_at),
            proposal.status is ProposalStatus.SENT,
        )
        for i, path in enumerate((proposal.file_path, meta.get("pdf"))):
            if path and Path(path).is_file():
                name = original if i == 0 and original else Path(path).name  # своё КП — под именем менеджера
                kp_files.append(FileView(f"kp{i}", name, Path(path).stat().st_size // 1024, True, None))

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

    raw_log = list(
        session.scalars(
            select(DealStatusLog).where(DealStatusLog.deal_id == deal.id).order_by(DealStatusLog.id.desc())
        )
    )
    history = [log_line(e) for e in raw_log]
    shown = {str(c.get("reason")) for c in score.get("conditions") or []}  # уже под плитками условий
    findings = list(
        dict.fromkeys(
            f.text
            for f in session.scalars(select(Finding).where(Finding.deal_id == deal.id).order_by(Finding.id))
            if f.text not in shown
        )
    )
    weights = score.get("weights") or {}
    catalog_total = score.get("catalog_total")
    pay = PAYMENT_RU[deal.payment_terms] + (f" {deal.payment_days} дн." if deal.payment_days else "")

    return DealCard(
        row=make_row(
            deal,
            platform.name if platform else "площадка",
            analysis.subject if analysis else deal.title,
            bool(kp_files),
            decided_at(session, [deal.id]).get(deal.id),
            now,
        ),
        url=deal.url,
        nmck=rub(deal.nmck),
        catalog_total=rub(Decimal(catalog_total)) if catalog_total else None,
        payment=pay,
        contract=CONTRACT_RU[deal.contract_format],
        has_tz=TRI_RU[deal.has_tz],
        smb_only=TRI_RU[deal.smb_only],
        min_kp_days=deal.kp_min_valid_days,
        conditions=[
            {**c, "short": SHORT.get(str(c.get("key")), str(c.get("title", ""))), "text": condition_text(c)}
            for c in score.get("conditions") or []
        ],
        why=list(expl.get("why") or []),
        check=expl.get("check"),
        why_stale=bool(expl.get("stale")),
        findings=findings,
        missing=[m for m in (analysis.missing if analysis else []) if m not in findings],
        positions=positions,
        not_license_reason=analysis.not_license_reason
        if analysis and not analysis.is_license_supply
        else None,
        kp_files=kp_files,
        kp_by_manager=bool(kp and kp.by_manager),
        kp=kp,
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
        steps_ru=f"{len(steps)} {plural(len(steps), ('шаг', 'шага', 'шагов'))}",
        history_ru=f"{len(history)} {plural(len(history), ('запись', 'записи', 'записей'))}",
        track=track(deal, raw_log, now),
        actions=next_actions(deal, bool(kp_files), now),
        can_skip=can_skip(deal, now),
        skip_reason=next(
            (
                e.reason or ""
                for e in raw_log
                if e.to_status is DealStatus.SKIPPED and e.actor is Actor.MANAGER
            ),
            "",
        ),
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
