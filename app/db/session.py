"""Движок и фабрика сессий. Создаются в create_app и передаются через app.state, без глобальных объектов."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_timeout=5)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def db_alive(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception:  # для /status важен только факт доступности
        return False
    return True


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session
