"""Вывод робота и штамп сделки — общие для карточки, списка, плагина, Excel и журнала."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from app.db.models import Actor, Channel, Deal, DealStatus, DealStatusLog
from app.scoring.score import CONDITIONS_TOTAL, catalog_met

MSK = timezone(timedelta(hours=3), "MSK")


STATUS_RU = {
    DealStatus.NEW: "в очереди на разбор",
    DealStatus.HOT: "горячая",
    DealStatus.ANALYZED: "разобрана",
    DealStatus.TAKEN: "в работе",
    DealStatus.SUBMITTED: "КП отправлено",
    DealStatus.SKIPPED: "отказ",
    DealStatus.EXPIRED: "приём закрыт",
    DealStatus.FILTERED_OUT: "отсеяна",
}
CHANNEL_RU = {
    Channel.STAND: "стенд",
    Channel.EXTENSION: "плагин",
    Channel.WORKER: "робот",
    Channel.EMAIL: "почта",
}


def msk(moment: datetime | None) -> str:
    return "не указано" if moment is None else moment.astimezone(MSK).strftime("%d.%m %H:%M")


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
    if not catalog_met(deal.score_breakdown) and deal.conditions_met >= CONDITIONS_TOTAL - 2:
        return Verdict.DECIDE  # позиции нет в каталоге — решает менеджер: искать у дистрибьютора или не идти
    if deal.status is DealStatus.HOT or deal.conditions_met == CONDITIONS_TOTAL:
        return Verdict.TAKE
    return Verdict.DECIDE if deal.conditions_met >= CONDITIONS_TOTAL - 1 else Verdict.SKIP


class Stamp(StrEnum):
    """Штамп сделки: решение менеджера, если оно есть, иначе вывод робота."""

    TAKE = "take"
    DECIDE = "decide"
    SKIP = "skip"
    WAIT = "wait"
    FILTERED = "filtered"
    WORK = "work"
    SENT = "sent"
    DECLINED = "declined"
    LATE = "late"
    CLOSED = "closed"


STAMP_RU = {
    Stamp.TAKE: "Стоит взять",
    Stamp.DECIDE: "Нужно ваше решение",
    Stamp.SKIP: "Не берём",
    Stamp.WAIT: "Ещё разбираем",
    Stamp.FILTERED: "Отсеяна",
    Stamp.WORK: "В работе",
    Stamp.SENT: "Отправлено",
    Stamp.DECLINED: "Отказ",
    Stamp.LATE: "Срок вышел",
    Stamp.CLOSED: "Приём закрыт",
}


def stamp(
    deal: Deal, v: Verdict, has_kp: bool, decided_at: datetime | None, now: datetime
) -> tuple[Stamp, str]:
    """Штамп и подпись под ним. Дата решения — время последней отметки менеджера (МСК)."""
    when = msk(decided_at) if decided_at else ""
    closed = deal.end_at is not None and deal.end_at <= now
    match deal.status:
        case DealStatus.SUBMITTED:
            return Stamp.SENT, when
        case DealStatus.SKIPPED:
            return Stamp.DECLINED, when
        case DealStatus.TAKEN if closed:
            return Stamp.LATE, "КП не отправлено"
        case DealStatus.TAKEN:
            return Stamp.WORK, f"с {when} · " + ("КП готово" if has_kp else "КП собирается")
        case DealStatus.FILTERED_OUT:
            return Stamp.FILTERED, "роботом по правилам"
    if deal.status is DealStatus.EXPIRED or closed:
        return Stamp.CLOSED, f"{msk(deal.end_at)}, КП не отправляли"
    sub = f"{deal.conditions_met} из {CONDITIONS_TOTAL} условий" if deal.conditions_met is not None else ""
    return Stamp(v.value), sub


@dataclass(frozen=True)
class LogLine:
    """Строка журнала сделки по-русски — для карточки, плагина и Excel."""

    at: datetime
    at_msk: str
    move: str
    who: str
    reason: str
    facts: str
    manager: bool


def log_line(e: DealStatusLog) -> LogLine:
    if e.actor is Actor.MANAGER:
        who = f"{e.actor_name}, {CHANNEL_RU[e.channel]}" + (f" «{e.device}»" if e.device else "")
    else:
        who = f"робот: {e.actor_name}"
    facts = []
    if e.conditions_met is not None:
        facts.append(f"{e.conditions_met} из {CONDITIONS_TOTAL}")
    if e.proposal_version is not None:
        facts.append(f"КП v{e.proposal_version}")
    if e.details.get("kp_file"):
        facts.append(f"файл {e.details['kp_file']}")
    if e.rules_version:
        facts.append(f"правила {e.rules_version}")
    return LogLine(
        at=e.at,
        at_msk=msk(e.at),
        # у решений, перенесённых из старой таблицы, прежний статус неизвестен — показываем только новый
        move=STATUS_RU[e.to_status]
        if e.from_status is None
        else f"{STATUS_RU[e.from_status]} → {STATUS_RU[e.to_status]}",
        who=who,
        reason=e.reason or "",
        facts=", ".join(facts),
        manager=e.actor is Actor.MANAGER,
    )
