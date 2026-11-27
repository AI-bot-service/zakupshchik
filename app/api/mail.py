"""Кнопка «Подготовить КП» из письма по сделке: без входа на стенд, по подписанной ссылке.

GET только показывает страницу: почтовые сканеры открывают ссылки сами, сделку от этого брать нельзя.
Заказ КП — POST с той же подписью: сделка «В работе», робот собирает КП в карточку сделки.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.auth import get_settings
from app.api.deals import apply_status
from app.core.settings import Settings
from app.db.models import Actor, Channel, Deal, DealStatus, Notification, NotifyChannel
from app.db.session import get_session
from app.notify.kp_link import kp_token_ok
from app.notify.letters import rub
from app.pipeline.proposals import kp_requested, latest_proposal, request_kp
from app.pipeline.status_log import Who
from app.report.stamps import MSK

router = APIRouter()
log = structlog.get_logger()
Db = Annotated[Session, Depends(get_session)]
Conf = Annotated[Settings, Depends(get_settings)]
TAKE_FROM = (DealStatus.ANALYZED, DealStatus.HOT, DealStatus.SKIPPED)
KEEP = (DealStatus.TAKEN, DealStatus.SUBMITTED)


def _letter(session: Session, settings: Settings, nid: int, token: str) -> tuple[Notification, Deal]:
    secret = settings.app_secret_key.get_secret_value()
    row = session.get(Notification, nid)
    if not kp_token_ok(secret, nid, token) or row is None or row.channel is not NotifyChannel.EMAIL_DEAL:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ссылка не подходит — откройте сделку на стенде")
    deal = session.get(Deal, row.deal_id) if row.deal_id is not None else None
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сделка удалена")
    return row, deal


def _state(session: Session, deal: Deal) -> str:
    """ready — КП лежит в карточке; building — заказано, робот собирает; none — не заказано."""
    p = latest_proposal(session, deal.id)
    if p is not None and p.file_path and Path(p.file_path).is_file():
        return "ready"
    return "building" if deal.status is DealStatus.TAKEN and kp_requested(deal) else "none"


def _can(deal: Deal, now: datetime) -> bool:
    closed = deal.end_at is not None and deal.end_at <= now
    return deal.conditions_met is not None and not closed and deal.status in (*TAKE_FROM, *KEEP)


@router.get("/mail/kp/{nid}/{token}", response_class=HTMLResponse)
def kp_page(request: Request, nid: int, token: str, session: Db, settings: Conf) -> HTMLResponse:
    _, deal = _letter(session, settings, nid, token)
    subject = ((deal.score_breakdown.get("analysis") or {}).get("result") or {}).get("subject") or deal.title
    ctx: dict[str, Any] = {
        "user": None,
        "deal": deal,
        "subject": subject,
        "nmck": rub(deal.nmck),
        "end_msk": f"{deal.end_at.astimezone(MSK):%d.%m %H:%M}" if deal.end_at else "срок не указан",
        "state": _state(session, deal),
        "can": _can(deal, datetime.now(UTC)),
        "base": f"/mail/kp/{nid}/{token}",
    }
    return request.app.state.templates.TemplateResponse(request, "mail_kp.html", ctx)  # type: ignore[no-any-return]


@router.post("/mail/kp/{nid}/{token}")
def kp_order(nid: int, token: str, session: Db, settings: Conf) -> RedirectResponse:
    row, deal = _letter(session, settings, nid, token)
    if not _can(deal, datetime.now(UTC)):
        raise HTTPException(status.HTTP_409_CONFLICT, "КП по этой сделке уже не собрать — откройте карточку")
    if deal.status in TAKE_FROM:
        who = Who(Actor.MANAGER, row.recipient, Channel.EMAIL)
        apply_status(session, deal, DealStatus.TAKEN, "кнопка «Подготовить КП» в письме", who)
    elif _state(session, deal) == "none":
        request_kp(deal)
        session.commit()
    log.info("kp_mail_ordered", deal_id=deal.id, notification_id=row.id)
    return RedirectResponse(f"/mail/kp/{nid}/{token}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/mail/kp/{nid}/{token}/state")
def kp_mail_state(nid: int, token: str, session: Db, settings: Conf) -> dict[str, str]:
    _, deal = _letter(session, settings, nid, token)
    return {"state": _state(session, deal)}
