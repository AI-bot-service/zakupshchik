"""Вкладка «Админка»: правила робота в БД. Сохранение = новая версия, пересчёт «N из 6» без токенов."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.auth import require_user
from app.api.deals import check_csrf, csrf_token
from app.db.models import RulesVersion
from app.db.session import get_session
from app.pipeline.analysis import rescore
from app.pipeline.prefilter import PROCEDURE_RU
from app.report.stamps import MSK
from app.scoring.rules import Rules, RulesError, RulesFieldError
from app.scoring.store import active_rules, history, save_rules
from app.web.rules_form import GROUPS, PROC_CHOICES, PROCEDURES, diff, form_values, label, parse_form

router = APIRouter(prefix="/admin")
log = structlog.get_logger()

User = Annotated[str, Depends(require_user)]
Db = Annotated[Session, Depends(get_session)]


def _page(
    request: Request,
    session: Session,
    user: str,
    values: dict[str, Any] | None = None,
    error: RulesError | None = None,
    note: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    rules = active_rules(session)
    session.commit()
    rows = history(session)
    versions = []
    for i, row in enumerate(rows):
        prev = rows[i + 1].profile if i + 1 < len(rows) else None
        versions.append(
            {
                "id": row.id,
                "version": row.version,
                "at": row.created_at.astimezone(MSK).strftime("%d.%m %H:%M"),
                "author": row.author,
                "note": row.note,
                "changes": diff(prev, row.profile),
                "current": i == 0,
            }
        )
    saved = request.query_params
    return request.app.state.templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "admin_rules.html",
        {
            "user": user,
            "csrf": csrf_token(request),
            "groups": GROUPS,
            "procedures": [(p.value, PROCEDURE_RU[p]) for p in PROCEDURES],
            "proc_choices": PROC_CHOICES,
            "values": values or form_values(rules.profile.model_dump(mode="json")),
            "version": rules.version,
            "versions": versions,
            "error_field": error.field.replace(".", "__") if isinstance(error, RulesFieldError) else None,
            "error": (
                f"«{label(error.field)}»: {error.message}"
                if isinstance(error, RulesFieldError)
                else str(error)
            )
            if error
            else None,
            "note": note,
            "saved": saved.get("saved"),
            "rescored": saved.get("rescored"),
            "changed": saved.get("changed"),
        },
        status_code=status_code,
    )


def _apply(session: Session, rules: Rules) -> dict[str, int]:
    stats = rescore(session, rules, datetime.now(UTC))
    session.commit()
    return stats


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, user: User, session: Db) -> HTMLResponse:
    return _page(request, session, user)


@router.post("/rules", response_model=None)
async def rules_save(request: Request, user: User, session: Db) -> HTMLResponse | RedirectResponse:
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    check_csrf(request, form.get("csrf", ""))
    note = form.get("note", "").strip()[:500]
    try:
        rules, changed = save_rules(session, parse_form(form), user, note)
    except RulesError as exc:
        session.rollback()
        return _page(request, session, user, form, exc, note, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not changed:
        return RedirectResponse("/admin/rules?saved=same", status_code=status.HTTP_303_SEE_OTHER)
    stats = _apply(session, rules)
    log.info("rules_applied", version=rules.version, user=user, **stats)
    return RedirectResponse(
        f"/admin/rules?saved={rules.version}&rescored={stats['rescored']}&changed={stats['changed']}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/rules/{row_id}/restore")
def rules_restore(
    request: Request, row_id: int, user: User, session: Db, csrf: Annotated[str, Form(max_length=64)]
) -> RedirectResponse:
    check_csrf(request, csrf)
    row = session.get(RulesVersion, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такой версии правил нет")
    rules, changed = save_rules(session, row.profile, user, f"возврат к версии {row.version}")
    if not changed:
        return RedirectResponse("/admin/rules?saved=same", status_code=status.HTTP_303_SEE_OTHER)
    stats = _apply(session, rules)
    return RedirectResponse(
        f"/admin/rules?saved={rules.version}&rescored={stats['rescored']}&changed={stats['changed']}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
