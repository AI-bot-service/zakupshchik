"""Сборка приложения. Все зависимости создаются здесь и кладутся в app.state."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.api import admin, auth, deals, ext, journal, mail, platforms, status
from app.core.logging import configure_logging
from app.core.provenance import BUILD_HEADER, BUILD_MARK
from app.core.settings import Settings
from app.db.session import make_engine, make_session_factory

WEB_DIR = Path(__file__).parent / "web"
MSK = timezone(timedelta(hours=3), "МСК")
VERSION = "0.1.0"


def format_msk(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()  # значения из окружения
    configure_logging(settings.debug)

    app = FastAPI(
        title="Закупщик",
        version=VERSION,
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url.get_secret_value().encoded_string())
    app.state.session_factory = make_session_factory(app.state.engine)
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.globals["build_mark"] = BUILD_MARK
    # версия стилей по содержимому: новый CSS — новый адрес, браузер не держит старый из кэша
    templates.env.globals["asset_v"] = hashlib.sha256(
        b"".join((WEB_DIR / "static" / f).read_bytes() for f in ("tokens.css", "app.css", "kp.js"))
    ).hexdigest()[:10]
    templates.env.filters["msk"] = format_msk
    templates.env.filters["dmy"] = lambda iso: f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"
    app.state.templates = templates

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key.get_secret_value(),
        session_cookie="zk_session",
        max_age=7 * 24 * 3600,
        same_site="lax",
        https_only=settings.https_only,
    )

    @app.middleware("http")
    async def build_header(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers[BUILD_HEADER] = BUILD_MARK
        return response

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(status.router)
    app.include_router(auth.router)
    app.include_router(deals.router)
    app.include_router(ext.router)
    app.include_router(ext.pages)
    app.include_router(admin.router)
    app.include_router(journal.router)
    app.include_router(platforms.router)
    app.include_router(mail.router)

    @app.get("/")
    def home(user: Annotated[str, Depends(auth.require_user)]) -> RedirectResponse:
        return RedirectResponse("/deals", status_code=303)

    return app
