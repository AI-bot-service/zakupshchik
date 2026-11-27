"""API плагина «Помощник менеджера» (§5, §7.3): входящие, разбор, файлы, решение. Вход — персональный ключ.

Ключ выпускается на странице стенда «Плагин», в БД лежит только sha256; новый ключ отзывает старый.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.analyze import load_prompt
from app.api.auth import require_user
from app.api.deals import apply_status, check_csrf, csrf_token, get_deal
from app.db.models import Actor, Channel, DealStatus, ExtKey, Manager
from app.db.session import get_session
from app.pipeline.status_log import Who
from app.report.stamps import msk
from app.scoring.rules import RulesError
from app.scoring.store import active_rules
from app.web.extension import build_zip, package
from app.web.view import View, deal_card, deal_rows, file_path, find_by_url

router = APIRouter(prefix="/api/ext")
pages = APIRouter()
log = structlog.get_logger()
KEY_PREFIX = "zk_"

Db = Annotated[Session, Depends(get_session)]
User = Annotated[str, Depends(require_user)]


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


KEY_TOUCH = timedelta(minutes=10)  # last_used_at обновляем не чаще: плагин опрашивает стенд раз в минуту


def issue_key(session: Session, name: str, label: str) -> str:
    """Новый ключ для устройства. Старые ключи менеджера продолжают работать, пока их не отозвали.

    Возвращается один раз, в БД — только sha256.
    """
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    manager = session.scalar(select(Manager).where(Manager.name == name))
    if manager is None:
        manager = Manager(name=name)
        session.add(manager)
        session.flush()
    session.add(
        ExtKey(manager_id=manager.id, key_hash=key_hash(key), label=label.strip()[:80] or "без подписи")
    )
    session.commit()
    log.info("ext_key_issued", manager=name)
    return key


def active_keys(session: Session, name: str) -> list[ExtKey]:
    return list(
        session.scalars(
            select(ExtKey)
            .join(Manager, Manager.id == ExtKey.manager_id)
            .where(Manager.name == name, ExtKey.revoked_at.is_(None))
            .order_by(ExtKey.id)
        )
    )


def require_manager(session: Db, authorization: Annotated[str, Header()] = "") -> ExtKey:
    scheme, _, key = authorization.partition(" ")
    found = None
    if scheme.lower() == "bearer" and key.startswith(KEY_PREFIX):
        found = session.scalar(
            select(ExtKey).where(ExtKey.key_hash == key_hash(key), ExtKey.revoked_at.is_(None))
        )
    if found is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Ключ не подошёл или отозван. Выпустите новый на странице стенда «Плагин» "
            "и вставьте в настройки.",
        )
    now = datetime.now(UTC)
    if found.last_used_at is None or now - found.last_used_at > KEY_TOUCH:
        found.last_used_at = now
        session.commit()
    return found


Mgr = Annotated[ExtKey, Depends(require_manager)]


def _rules_info(session: Session) -> tuple[int, str]:
    """Порог уведомления и версия действующих правил."""
    try:
        rules = active_rules(session)
        session.commit()
        return rules.profile.notify.min_conditions, rules.version
    except (RulesError, ValueError, OSError):
        return 5, "—"


def _agent_version() -> str:
    """Версия агента разбора = версия промпта (хэш файла prompts/analyze.md)."""
    try:
        return load_prompt("analyze").version
    except OSError:
        return "—"


@router.get("/inbox")
def inbox(request: Request, session: Db, manager: Mgr) -> dict[str, Any]:
    """Сделки с открытым приёмом КП. `notify` — порог из действующих правил, решённые не уведомляем."""
    now = datetime.now(UTC)
    min_n, rules_version = _rules_info(session)
    rows = deal_rows(session, View.OPEN, now)
    done = (DealStatus.TAKEN, DealStatus.SUBMITTED, DealStatus.SKIPPED)
    return {
        "manager": manager.manager.name,
        "build": package(request.app.state.settings.public_url)[0],
        "notify_min": min_n,
        "agent": _agent_version(),
        "rules": rules_version,
        "checked_at": now.isoformat(),
        "deals": [asdict(r) | {"notify": (r.n or 0) >= min_n and r.status not in done} for r in rows],
    }


@router.get("/package")
def extension_package(request: Request, manager: Mgr) -> dict[str, Any]:
    """Файлы текущей сборки плагина (base64) — плагин перезаписывает ими свою папку и перезапускается."""
    build, files = package(request.app.state.settings.public_url)
    log.info("ext_package", manager=manager.manager.name, build=build)
    return {"build": build, "files": {rel: base64.b64encode(data).decode() for rel, data in files.items()}}


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
    who = Who(Actor.MANAGER, manager.manager.name, Channel.EXTENSION, manager.label)
    deal = apply_status(session, get_deal(session, deal_id), body.action, body.reason, who)
    log.info("deal_status", deal_id=deal.id, action=body.action.value, manager=who.name, via="ext")
    return {"status": deal.status.value}


def _page_ctx(request: Request, session: Session, user: str, key: str | None) -> dict[str, Any]:
    build, files = package(request.app.state.settings.public_url)
    version = json.loads(files["manifest.json"])["version"]
    return {
        "user": user,
        "csrf": csrf_token(request),
        "issue_nonce": _new_issue_nonce(request),
        "key": key,
        "keys": [
            {
                "id": k.id,
                "label": k.label,
                "created": msk(k.created_at),
                "used": f"на связи {msk(k.last_used_at)}" if k.last_used_at else "ещё не подключался",
                "seen": k.last_used_at is not None,
            }
            for k in active_keys(session, user)
        ],
        "notify_min": _rules_info(session)[0],
        "version": version,
        "build": build,
    }


ISSUE_NONCE_KEY = "ext_issue_nonce"


def _new_issue_nonce(request: Request) -> str:
    """Одноразовый токен формы выпуска: повторная отправка той же формы (F5) ключ не выпускает."""
    nonce = secrets.token_urlsafe(16)
    request.session[ISSUE_NONCE_KEY] = nonce
    return nonce


@pages.get("/extension", response_class=HTMLResponse)
def extension_page(request: Request, user: User, session: Db) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "extension.html",
        _page_ctx(request, session, user, None),
    )


@pages.post("/extension/key", response_class=HTMLResponse)
def extension_key(
    request: Request,
    user: User,
    session: Db,
    csrf: Annotated[str, Form(max_length=64)],
    nonce: Annotated[str, Form(max_length=64)] = "",
    label: Annotated[str, Form(max_length=80)] = "",
) -> Response:
    check_csrf(request, csrf)
    expected = request.session.pop(ISSUE_NONCE_KEY, None)
    if not isinstance(expected, str) or not secrets.compare_digest(expected, nonce):
        return RedirectResponse("/extension", status_code=status.HTTP_303_SEE_OTHER)
    key = issue_key(session, user, label)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "extension.html",
        _page_ctx(request, session, user, key),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@pages.post("/extension/key/{key_id}/revoke")
def extension_key_revoke(
    request: Request, key_id: int, user: User, session: Db, csrf: Annotated[str, Form(max_length=64)]
) -> RedirectResponse:
    """Отозвать ключ одного устройства; остальные продолжают работать."""
    check_csrf(request, csrf)
    found = next((k for k in active_keys(session, user) if k.id == key_id), None)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ключ не найден или уже отозван")
    found.revoked_at = datetime.now(UTC)
    session.commit()
    log.info("ext_key_revoked", manager=user, key_id=key_id)
    return RedirectResponse("/extension", status_code=status.HTTP_303_SEE_OTHER)


@pages.get("/extension.zip")
def extension_zip(request: Request, user: User) -> Response:
    data = build_zip(request.app.state.settings.public_url)
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="zakupshchik-extension.zip"'},
    )
