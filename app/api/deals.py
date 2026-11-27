"""Страница «Сделки», карточка, статусы менеджера, файлы сделки и Excel за период (§7.3, §11)."""

from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.auth import require_user
from app.db.models import Actor, Channel, Deal, DealStatus, Platform, ProposalStatus
from app.db.session import get_session
from app.pipeline.proposals import KP_MAX_BYTES, KP_SUFFIXES, attach_manager_kp, latest_proposal, request_kp
from app.pipeline.status_log import Who, change_status
from app.report.excel import ALL_TIME, build_report, day_period
from app.report.stamps import MSK
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
    view_filter,
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


NO_KP = (
    "КП в карточке нет. Нажмите «Беру в работу» — робот соберёт КП, "
    "или приложите своё КП в карточке на стенде."
)


def apply_status(
    session: Session,
    deal: Deal,
    action: DealStatus,
    reason: str,
    who: Who,
    details: dict[str, Any] | None = None,
) -> Deal:
    """Решение менеджера со стенда или из плагина: статус сделки + строка в журнале сделки.

    Повтор того же решения (второй «Беру в работу») ничего не меняет и в журнал не пишется: КП уже заказано.
    Исключения: «Не беру» с новой причиной и новое своё КП (details) — пишутся.
    «КП отправлено» без КП в карточке — 409: сначала собрать КП или приложить своё.
    «Вернуть в работу» из «КП отправлено» снимает с последнего КП отметку «отправлено», файл остаётся.
    """
    if action not in MANAGER_ACTIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Такого статуса нет")
    reason = reason.strip()
    if action is DealStatus.SKIPPED and not reason:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Укажите причину отказа")
    if action is DealStatus.TAKEN and deal.conditions_met is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Сделка ещё не разобрана роботом — КП собрать не из чего"
        )
    again = action is deal.status and (action is DealStatus.SKIPPED or bool(details))
    if action is deal.status and not again:
        return deal
    proposal = latest_proposal(session, deal.id)
    if action is DealStatus.SUBMITTED:
        if proposal is None:
            raise HTTPException(status.HTTP_409_CONFLICT, NO_KP)
        proposal.status = ProposalStatus.SENT
    if action is DealStatus.TAKEN:
        if (
            deal.status is DealStatus.SUBMITTED
            and proposal is not None
            and proposal.status is ProposalStatus.SENT
        ):
            proposal.status = (
                ProposalStatus.DRAFT
            )  # «Вернуть в работу»: КП остаётся, но отправленным не числится
        request_kp(deal)  # воркер соберёт КП по разбору в течение ~10 с, даже если сделка не на 100%
    change_status(session, deal, action, who, reason, proposal, details, always=again)
    session.commit()
    return deal


def stand(user: str) -> Who:
    return Who(Actor.MANAGER, user, Channel.STAND)


def get_deal(session: Session, deal_id: int) -> Deal:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сделка не найдена")
    return deal


MAX_RANGE_DAYS = 366
DEFAULT_DAYS = 7


def _range(first: date | None, last: date | None, today: date) -> tuple[date, date]:
    """Период по дате размещения: не задан — последние 7 дней; перепутанные даты меняем местами."""
    last = min(last or today, today)
    first = first or last - timedelta(days=DEFAULT_DAYS - 1)
    if first > last:
        first, last = last, first
    if (last - first).days >= MAX_RANGE_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Период не длиннее года")
    return first, last


def _day(raw: str | None) -> date | None:
    """Пустое поле даты из формы (from=) — «не задано», а не ошибка."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Дата в формате ГГГГ-ММ-ДД") from exc


def _period(view: View, first: str | None, last: str | None, today: date) -> tuple[date, date] | None:
    """Даты не заданы: «Архив» — последние 7 дней, остальные вкладки — без ограничения по датам."""
    a, b = _day(first), _day(last)
    if a is None and b is None and view is not View.ARCHIVE:
        return None
    return _range(a, b, today)


@router.get("/deals", response_class=HTMLResponse)
def deals_page(
    request: Request,
    user: User,
    session: Db,
    view: View = View.OPEN,
    first: Annotated[str | None, Query(alias="from", max_length=10)] = None,
    last: Annotated[str | None, Query(alias="to", max_length=10)] = None,
) -> HTMLResponse:
    now = datetime.now(UTC)
    today = now.astimezone(MSK).date()
    span = _period(view, first, last, today)
    period = day_period(*span) if span else None
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
            "first": span[0].isoformat() if span else "",
            "last": span[1].isoformat() if span else "",
            "period_dates": period.title.removeprefix("размещены ") if period else None,
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
    try:
        deal = apply_status(session, get_deal(session, deal_id), action, reason, stand(user))
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        return RedirectResponse(f"/deals/{deal_id}?need_kp=1#decide", status_code=status.HTTP_303_SEE_OTHER)
    log.info("deal_status", deal_id=deal.id, action=action.value, user=user)
    return RedirectResponse(f"/deals/{deal.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/deals/{deal_id}/kp")
async def upload_kp(
    request: Request,
    deal_id: int,
    user: User,
    session: Db,
    kp: Annotated[UploadFile, File()],
    csrf: Annotated[str, Form(max_length=64)],
) -> RedirectResponse:
    """Своё КП менеджера: кладём в карточку новой версией и сразу отмечаем «КП отправлено»."""
    check_csrf(request, csrf)
    deal = get_deal(session, deal_id)
    name = kp.filename or ""
    if Path(name).suffix.lower() not in KP_SUFFIXES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "КП в формате DOCX, DOC, PDF, ODT или XLSX"
        )
    data = await kp.read(KP_MAX_BYTES + 1)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Файл пустой")
    if len(data) > KP_MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Файл больше 20 МБ")
    platform = session.get(Platform, deal.platform_id)
    attach_manager_kp(session, deal, platform.code if platform else "platform", name, data, datetime.now(UTC))
    session.flush()
    details = {"kp_file": Path(name).name[:200], "kp_bytes": len(data), "kp_source": "manager"}
    apply_status(session, deal, DealStatus.SUBMITTED, "своё КП менеджера", stand(user), details)
    log.info("deal_kp_uploaded", deal_id=deal.id, size=len(data), user=user)
    return RedirectResponse(f"/deals/{deal.id}", status_code=status.HTTP_303_SEE_OTHER)


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
    view: View = View.ARCHIVE,
    first: Annotated[str | None, Query(alias="from", max_length=10)] = None,
    last: Annotated[str | None, Query(alias="to", max_length=10)] = None,
) -> Response:
    """Excel ровно того, что на экране: та же вкладка и тот же период по дате размещения."""
    now = datetime.now(UTC)
    span = _period(view, first, last, now.astimezone(MSK).date())
    try:
        rules = active_rules(session)
        session.commit()
    except (RulesError, ValueError, OSError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Правила не читаются") from exc
    period = day_period(*span) if span else ALL_TIME
    period = replace(period, title=f"вкладка «{VIEW_RU[view]}», {period.title}")
    data, _ = build_report(session, rules, now, period, view_filter(view, now))
    if span is None:
        dates = f"{now.astimezone(MSK):%Y-%m-%d}"
    elif span[0] == span[1]:
        dates = f"{span[0]:%Y-%m-%d}"
    else:
        dates = f"{span[0]:%Y-%m-%d}_{span[1]:%Y-%m-%d}"
    name = f"zakupshchik-{view.value}-{dates}.xlsx"
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
