"""Вкладка «Журнал»: последние прогоны робота и кнопка «Запустить цикл» вне расписания."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import require_user
from app.api.deals import check_csrf, csrf_token
from app.db.models import Deal, Platform
from app.db.session import get_session
from app.pipeline import demo
from app.pipeline.runs import STATE_RU, active_manual, recent_runs, request_run
from app.report.stamps import MSK

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
            "demo_rows": [
                (r.id, r.output | {"at": datetime.fromisoformat(r.output["at"])})
                for r in demo.recent(session)
            ],
            "demo_msg": request.query_params.get("demo"),
            "demo_count": session.scalar(select(func.count()).where(Deal.raw_masked.has_key("demo"))),
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


def _release_at(value: str, now: datetime) -> datetime:
    """«14:32» — сегодня по Москве (прошло — завтра); пусто — через 10 секунд."""
    if not value.strip():
        return now + timedelta(seconds=10)
    hh, mm = (int(x) for x in value.strip().split(":", 1))
    local = datetime.combine(now.astimezone(MSK).date(), time(hh, mm), MSK)
    if local < now - timedelta(minutes=1):
        local += timedelta(days=1)
    return local.astimezone(UTC)


@router.post("/journal/demo")
def journal_demo(
    request: Request,
    user: User,
    session: Db,
    csrf: Annotated[str, Form(max_length=64)],
    source: Annotated[int, Form(ge=1)],
    at: Annotated[str, Form(max_length=5)] = "",
) -> RedirectResponse:
    check_csrf(request, csrf)
    now = datetime.now(UTC)
    try:
        row = demo.schedule(session, user, source, _release_at(at, now), now)
    except (demo.DemoError, ValueError) as exc:
        session.rollback()
        log.info("demo_rejected", user=user, source=source, reason=str(exc))
        return RedirectResponse("/journal?demo=bad#demo", status_code=status.HTTP_303_SEE_OTHER)
    session.commit()
    log.info("demo_scheduled", user=user, row=row.id, source=source, at=row.output["at"])
    return RedirectResponse("/journal?demo=ok#demo", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/journal/demo/{row_id}/cancel")
def journal_demo_cancel(
    request: Request, user: User, session: Db, row_id: int, csrf: Annotated[str, Form(max_length=64)]
) -> RedirectResponse:
    check_csrf(request, csrf)
    demo.cancel(session, row_id)
    session.commit()
    return RedirectResponse("/journal#demo", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/journal/demo/purge")
def journal_demo_purge(
    request: Request, user: User, session: Db, csrf: Annotated[str, Form(max_length=64)]
) -> RedirectResponse:
    check_csrf(request, csrf)
    removed = demo.purge(session)
    session.commit()
    log.info("demo_purged", user=user, deals=removed)
    return RedirectResponse("/journal?demo=purged#demo", status_code=status.HTTP_303_SEE_OTHER)
