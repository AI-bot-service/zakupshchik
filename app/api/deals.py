"""Страница «Сделки», карточка, статусы менеджера, файлы сделки и Excel за период (§7.3, §11)."""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.auth import require_user
from app.db.models import Deal, DealStatus, Feedback
from app.db.session import get_session
from app.pipeline.proposals import request_kp
from app.report.excel import MSK, build_report, day_period
from app.scoring.rules import RulesError
from app.scoring.store import active_rules
from app.web.view import (
    ACTION_RU,
    MANAGER_ACTIONS,
    VIEW_RU,
    View,
    deal_card,
    deal_rows,
    file_path,
    view_counts,
)

router = APIRouter()
log = structlog.get_logger()
CSRF_KEY = "csrf"

User = Annotated[str, Depends(require_user)]
Db = Annotated[Session, Depends(get_session)]


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(24)
        request.session[CSRF_KEY] = token
    return token


def check_csrf(request: Request, token: str) -> None:
    expected = request.session.get(CSRF_KEY)
    if not isinstance(expected, str) or not secrets.compare_digest(expected, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Форма устарела, обновите страницу")


def apply_status(
    session: Session, deal: Deal, action: DealStatus, reason: str, manager_id: int | None
) -> Deal:
    """Решение менеджера со стенда, из плагина или письма: статус сделки + запись в feedback."""
    if action not in MANAGER_ACTIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Такого статуса нет")
    reason = reason.strip()
    if action is DealStatus.SKIPPED and not reason:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Напишите, почему не берёте сделку")
    deal.status = action
    if action is DealStatus.TAKEN:
        request_kp(deal)  # воркер соберёт КП по разбору в течение ~10 с, даже если сделка не на 100%
    session.add(Feedback(deal_id=deal.id, manager_id=manager_id, action=action, reason=reason or None))
    session.commit()
    return deal


def get_deal(session: Session, deal_id: int) -> Deal:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сделка не найдена")
    return deal


MAX_RANGE_DAYS = 366


def _range(first: date | None, last: date | None, today: date) -> tuple[date, date]:
    """Период архива и выгрузки: по умолчанию последние 7 дней; перепутанные даты меняем местами."""
    last = min(last or today, today)
    first = first or last - timedelta(days=6)
    if first > last:
        first, last = last, first
    if (last - first).days >= MAX_RANGE_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Период не длиннее года")
    return first, last


@router.get("/deals", response_class=HTMLResponse)
def deals_page(
    request: Request,
    user: User,
    session: Db,
    view: View = View.OPEN,
    first: Annotated[date | None, Query(alias="from")] = None,
    last: Annotated[date | None, Query(alias="to")] = None,
) -> HTMLResponse:
    now = datetime.now(UTC)
    today = now.astimezone(MSK).date()
    first, last = _range(first, last, today)
    period = day_period(first, last)
    return request.app.state.templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "deals.html",
        {
            "user": user,
            "view": view,
            "views": VIEW_RU,
            "counts": view_counts(session, now, period),
            "rows": deal_rows(session, view, now, period),
            "today": today.isoformat(),
            "first": first.isoformat(),
            "last": last.isoformat(),
            "period_title": period.title,
        },
    )


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(request: Request, deal_id: int, user: User, session: Db) -> HTMLResponse:
    card = deal_card(session, get_deal(session, deal_id), datetime.now(UTC))
    return request.app.state.templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "deal.html",
        {
            "user": user,
            "card": card,
            "actions": [(a.value, ACTION_RU[a]) for a in MANAGER_ACTIONS],
            "csrf": csrf_token(request),
        },
    )


@router.post("/deals/{deal_id}/status")
def set_status(
    request: Request,
    deal_id: int,
    user: User,
    session: Db,
    action: Annotated[DealStatus, Form()],
    csrf: Annotated[str, Form(max_length=64)],
    reason: Annotated[str, Form(max_length=500)] = "",
) -> RedirectResponse:
    check_csrf(request, csrf)
    deal = apply_status(session, get_deal(session, deal_id), action, reason, None)
    log.info("deal_status", deal_id=deal.id, action=action.value, user=user)
    return RedirectResponse(f"/deals/{deal.id}?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/deals/{deal_id}/files/{file_id}")
def deal_file(deal_id: int, file_id: str, user: User, session: Db) -> FileResponse:
    path = file_path(session, get_deal(session, deal_id), file_id)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файла нет на стенде — откройте закупку на площадке")
    return FileResponse(path, filename=path.name, content_disposition_type="attachment")


@router.get("/deals.xlsx")
def export(
    user: User,
    session: Db,
    first: Annotated[date | None, Query(alias="from")] = None,
    last: Annotated[date | None, Query(alias="to")] = None,
) -> Response:
    """Excel за дату или период: сделки, найденные в эти дни, со статусами на момент выгрузки."""
    now = datetime.now(UTC)
    first, last = _range(first, last, now.astimezone(MSK).date())
    try:
        rules = active_rules(session)
        session.commit()
    except (RulesError, ValueError, OSError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Правила не читаются") from exc
    data, _ = build_report(session, rules, now, day_period(first, last))
    span = f"{first:%Y-%m-%d}" if first == last else f"{first:%Y-%m-%d}_{last:%Y-%m-%d}"
    name = f"zakupshchik-{span}.xlsx"
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
