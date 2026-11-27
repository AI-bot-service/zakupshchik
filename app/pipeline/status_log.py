"""Смена статуса сделки — только здесь: статус + строка в журнале этой сделки (deal_status_log).

В строке: было → стало, кто (менеджер по логину или шаг робота), откуда (стенд, плагин с подписью ключа,
воркер), причина, «N из 6» и версия правил на этот момент, какое КП было в карточке (id и версия),
подробности (JSON).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Actor, Channel, Deal, DealStatus, DealStatusLog, Proposal


@dataclass(frozen=True)
class Who:
    actor: Actor
    name: str
    channel: Channel
    device: str | None = None


def robot(step: str) -> Who:
    return Who(Actor.ROBOT, step, Channel.WORKER)


def change_status(
    session: Session,
    deal: Deal,
    to: DealStatus,
    who: Who,
    reason: str | None = None,
    proposal: Proposal | None = None,
    details: dict[str, Any] | None = None,
    always: bool = False,
) -> bool:
    """Статус → to и строка журнала. Тот же статус — ничего не пишем (False), если не always."""
    before = deal.status
    if before is to and not always:
        return False
    deal.status = to
    session.add(
        DealStatusLog(
            deal=deal,
            from_status=before,
            to_status=to,
            actor=who.actor,
            actor_name=who.name[:255],
            channel=who.channel,
            device=who.device,
            reason=reason or None,
            conditions_met=deal.conditions_met,
            rules_version=deal.rules_version,
            proposal_id=proposal.id if proposal is not None else None,
            proposal_version=proposal.version if proposal is not None else None,
            details=details or {},
        )
    )
    return True


def decided_at(session: Session, ids: list[int]) -> dict[int, datetime]:
    """Время последнего решения менеджера по сделке — дата на штампе."""
    q = (
        select(DealStatusLog.deal_id, func.max(DealStatusLog.at))
        .where(DealStatusLog.deal_id.in_(ids), DealStatusLog.actor == Actor.MANAGER)
        .group_by(DealStatusLog.deal_id)
    )
    return dict(session.execute(q).tuples().all())
