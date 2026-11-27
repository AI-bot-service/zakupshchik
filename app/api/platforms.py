"""Вкладка «Площадки»: какие площадки опрашивает робот. Открытые (free) — сервер без входа,
закрытые (авторизация) — плагин в браузере менеджера под его учёткой."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.adapters.config import load_registry
from app.api.auth import require_user
from app.api.deals import check_csrf, csrf_token
from app.collector import PLATFORMS_FILE
from app.db.models import AdapterKind, Deal, Platform, PlatformStatus
from app.db.session import get_session
from app.pipeline.ingest import sync_platforms

router = APIRouter()
log = structlog.get_logger()

User = Annotated[str, Depends(require_user)]
Db = Annotated[Session, Depends(get_session)]


def _sync(session: Session) -> None:
    registry = load_registry(PLATFORMS_FILE)
    sync_platforms(session, registry.platforms, registry.browser_platforms)


def platform_rows(session: Session) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    counts = {
        pid: (total, open_)
        for pid, total, open_ in session.execute(
            select(
                Deal.platform_id,
                func.count(),
                func.count(case((Deal.end_at > now, 1))),
            ).group_by(Deal.platform_id)
        )
    }
    rows = []
    for p in session.scalars(select(Platform).order_by(Platform.adapter_kind, Platform.id)):
        auth = p.adapter_kind is AdapterKind.EXTENSION
        total, open_ = counts.get(p.id, (0, 0))
        rows.append(
            {
                "code": p.code,
                "name": p.name,
                "url": p.config.get("search_url") or p.config.get("site_url") or p.config.get("url", ""),
                "auth": auth,
                "on": p.status is PlatformStatus.ON,
                "note": p.config.get("note", ""),
                "total": total,
                "open": open_,
                "last_ok_at": p.last_ok_at,
                "last_error": p.last_error,
            }
        )
    return rows


@router.get("/platforms", response_class=HTMLResponse)
def platforms_page(request: Request, user: User, session: Db) -> HTMLResponse:
    _sync(session)
    rows = platform_rows(session)
    session.commit()
    return request.app.state.templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "platforms.html",
        {
            "user": user,
            "csrf": csrf_token(request),
            "rows": rows,
            "on_free": sum(1 for r in rows if r["on"] and not r["auth"]),
            "on_auth": sum(1 for r in rows if r["on"] and r["auth"]),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/platforms")
async def platforms_save(request: Request, user: User, session: Db) -> RedirectResponse:
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    chosen = {v for v in form.getlist("on") if isinstance(v, str)}
    changed = []
    for p in session.scalars(select(Platform)):
        new = PlatformStatus.ON if p.code in chosen else PlatformStatus.OFF
        if p.status is not new:
            p.status = new
            changed.append(f"{p.code}={new.value}")
    session.commit()
    log.info("platforms_saved", user=user, changed=changed)
    return RedirectResponse(f"/platforms?saved={len(changed)}", status_code=status.HTTP_303_SEE_OTHER)
