"""Вход на стенд. Учётка «Жюри» из окружения; пустой пароль закрывает вход."""

from __future__ import annotations

import secrets
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.settings import Settings

router = APIRouter()
log = structlog.get_logger()

SESSION_USER = "user"


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def check_credentials(settings: Settings, login: str, password: str) -> bool:
    expected = settings.jury_password.get_secret_value()
    if not expected:
        return False
    login_ok = secrets.compare_digest(login.encode(), settings.jury_login.encode())
    password_ok = secrets.compare_digest(password.encode(), expected.encode())
    return login_ok and password_ok


def current_user(request: Request) -> str | None:
    user = request.session.get(SESSION_USER)
    return user if isinstance(user, str) else None


def require_user(request: Request) -> str:
    user = current_user(request)
    if user is None:
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": None})  # type: ignore[no-any-return]


@router.post("/login", response_model=None)
def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    login: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=256)],
) -> HTMLResponse | RedirectResponse:
    if not check_credentials(settings, login, password):
        log.info("login_failed")
        templates = request.app.state.templates
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request,
            "login.html",
            {"error": "Логин или пароль не подходят. Данные для входа — в форме сдачи проекта."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session.clear()
    request.session[SESSION_USER] = login
    log.info("login_ok", user=login)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
