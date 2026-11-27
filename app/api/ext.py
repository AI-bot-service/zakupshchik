"""API плагина «Помощник менеджера» (§5, §7.3): входящие, разбор, файлы, решение. Вход — персональный ключ.

Ключ выпускается на странице стенда «Плагин», в БД лежит только sha256; новый ключ отзывает старый.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_user
from app.api.deals import apply_status, check_csrf, csrf_token, get_deal
from app.db.models import DealStatus, Manager
from app.db.session import get_session
from app.scoring.rules import RulesError, load_rules
from app.web.extension import build_zip
from app.web.view import View, deal_card, deal_rows, file_path, find_by_url

router = APIRouter(prefix="/api/ext")
pages = APIRouter()
log = structlog.get_logger()
KEY_PREFIX = "zk_"

Db = Annotated[Session, Depends(get_session)]
User = Annotated[str, Depends(require_user)]


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def issue_key(session: Session, name: str) -> str:
    """Новый ключ менеджеру `name` (создаётся при первом выпуске). Возвращается один раз, хранится хэш."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    manager = session.scalar(select(Manager).where(Manager.name == name))
    if manager is None:
        manager = Manager(name=name)
        session.add(manager)
    manager.ext_key_hash = key_hash(key)
    session.commit()
    log.info("ext_key_issued", manager=name)
    return key


def require_manager(session: Db, authorization: Annotated[str, Header()] = "") -> Manager:
    scheme, _, key = authorization.partition(" ")
    manager = None
    if scheme.lower() == "bearer" and key.startswith(KEY_PREFIX):
        manager = session.scalar(select(Manager).where(Manager.ext_key_hash == key_hash(key)))
    if manager is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Ключ не подошёл. Выпустите новый на странице стенда «Плагин» и вставьте в настройки.",
        )
    return manager


Mgr = Annotated[Manager, Depends(require_manager)]


def _notify_min() -> int:
    try:
        return load_rules().profile.notify.min_conditions
    except (RulesError, ValueError, OSError):
        return 5


@router.get("/inbox")
def inbox(session: Db, manager: Mgr) -> dict[str, Any]:
    """Сделки с открытым приёмом КП. `notify` — порог уведомления из rules.yaml, решённые не уведомляем."""
    now = datetime.now(UTC)
    min_n = _notify_min()
    rows = deal_rows(session, View.OPEN, now)
    done = (DealStatus.TAKEN, DealStatus.SUBMITTED, DealStatus.SKIPPED)
    return {
        "manager": manager.name,
        "notify_min": min_n,
        "checked_at": now.isoformat(),
        "deals": [asdict(r) | {"notify": (r.n or 0) >= min_n and r.status not in done} for r in rows],
    }


@router.get("/deals/{deal_id}")
def deal(deal_id: int, session: Db, manager: Mgr) -> dict[str, Any]:
    return asdict(deal_card(session, get_deal(session, deal_id), datetime.now(UTC)))


@router.get("/lookup")
def lookup(session: Db, manager: Mgr, url: Annotated[str, Query(max_length=2000)]) -> dict[str, Any]:
    """Открытая вкладка — закупка из нашей базы? Ответ: id или null (тогда панель показывает список)."""
    found = find_by_url(session, url)
    return {"deal_id": found.id if found else None}


@router.get("/deals/{deal_id}/files/{file_id}")
def deal_file(deal_id: int, file_id: str, session: Db, manager: Mgr) -> FileResponse:
    path = file_path(session, get_deal(session, deal_id), file_id)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файла нет на стенде — откройте закупку на площадке")
    return FileResponse(path, filename=path.name, content_disposition_type="attachment")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: DealStatus
    reason: str = Field(default="", max_length=500)


@router.post("/deals/{deal_id}/status")
def decide(deal_id: int, body: Decision, session: Db, manager: Mgr) -> dict[str, str]:
    deal = apply_status(session, get_deal(session, deal_id), body.action, body.reason, manager.id)
    log.info("deal_status", deal_id=deal.id, action=body.action.value, manager=manager.name, via="ext")
    return {"status": deal.status.value}


@pages.get("/extension", response_class=HTMLResponse)
def extension_page(request: Request, user: User) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "extension.html",
        {"user": user, "csrf": csrf_token(request), "key": None, "notify_min": _notify_min()},
    )


@pages.post("/extension/key", response_class=HTMLResponse)
def extension_key(
    request: Request, user: User, session: Db, csrf: Annotated[str, Form(max_length=64)]
) -> HTMLResponse:
    check_csrf(request, csrf)
    key = issue_key(session, user)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "extension.html",
        {"user": user, "csrf": csrf_token(request), "key": key, "notify_min": _notify_min()},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@pages.get("/extension.zip")
def extension_zip(request: Request, user: User) -> Response:
    data = build_zip(request.app.state.settings.public_url)
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="zakupshchik-extension.zip"'},
    )
