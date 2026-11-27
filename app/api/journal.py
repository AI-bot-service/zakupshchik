"""Вкладка «Журнал»: последние прогоны робота и кнопка «Запустить цикл» вне расписания."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_user
from app.api.deals import check_csrf, csrf_token
from app.db.models import Platform
from app.db.session import get_session
from app.pipeline.runs import STATE_RU, active_manual, recent_runs, request_run

router = APIRouter()
log = structlog.get_logger()

User = Annotated[str, Depends(require_user)]
Db = Annotated[Session, Depends(get_session)]


@router.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request, user: User, session: Db) -> HTMLResponse:
    now = datetime.now(UTC)
    runs = recent_runs(session, limit=30)
    active = active_manual(session, now)
    # «ТЭК-Торг — Интернет-магазин (…)» → «ТЭК-Торг»: в таблице хватает короткого имени
    names = {p.code: p.name.split(" — ")[0] for p in session.scalars(select(Platform))}
    return request.app.state.templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "journal.html",
        {
            "user": user,
            "csrf": csrf_token(request),
            "runs": runs,
            "names": names,
            "state_ru": STATE_RU,
            "active": active,
            "started": request.query_params.get("started"),
        },
    )


@router.post("/journal/run")
def journal_run(
    request: Request, user: User, session: Db, csrf: Annotated[str, Form(max_length=64)]
) -> RedirectResponse:
    check_csrf(request, csrf)
    row, created = request_run(session, user, datetime.now(UTC))
    session.commit()
    log.info("manual_run_requested", user=user, row=row.id, created=created)
    return RedirectResponse(
        f"/journal?started={'new' if created else 'already'}", status_code=status.HTTP_303_SEE_OTHER
    )
