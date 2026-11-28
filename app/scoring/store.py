"""Действующие правила в БД: чтение, новая версия, откат. Файл rules.yaml — только первая версия."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RulesVersion
from app.scoring.rules import RULES_FILE, Rules, load_rules, make_rules, parse_profile

log = structlog.get_logger()
SEED_AUTHOR = "исходные значения (config/rules.yaml)"


def latest(session: Session) -> RulesVersion | None:
    return session.scalars(select(RulesVersion).order_by(RulesVersion.id.desc()).limit(1)).first()


def active_rules(session: Session) -> Rules:
    """Последняя версия; таблица пуста — rules.yaml становится первой версией (коммитит вызывающий)."""
    row = latest(session)
    if row is None:
        rules = load_rules(RULES_FILE)
        session.add(
            RulesVersion(
                version=rules.version,
                profile=rules.profile.model_dump(mode="json"),
                author=SEED_AUTHOR,
                note="первая версия",
            )
        )
        session.flush()
        log.info("rules_seeded", version=rules.version)
        return rules
    return make_rules(parse_profile(row.profile, f"версия правил {row.version}"))


def save_rules(session: Session, data: Any, author: str, note: str | None) -> tuple[Rules, bool]:  # noqa: ANN401
    """Проверить и сохранить новой версией. Возвращает (правила, изменились ли). Ошибка — RulesError."""
    rules = make_rules(parse_profile(data))
    current = latest(session)
    # Версия без адресатов: сравниваем ещё и профиль, иначе смена адресов не сохранится
    if (
        current is not None
        and current.version == rules.version
        and parse_profile(current.profile) == rules.profile
    ):
        return rules, False
    session.add(
        RulesVersion(
            version=rules.version,
            profile=rules.profile.model_dump(mode="json"),
            author=author,
            note=note or None,
        )
    )
    session.flush()
    log.info("rules_saved", version=rules.version, author=author)
    return rules, True


def history(session: Session, limit: int = 20) -> list[RulesVersion]:
    return list(session.scalars(select(RulesVersion).order_by(RulesVersion.id.desc()).limit(limit)))


def recipients_since(session: Session) -> dict[str, datetime]:
    """Адрес → с какой версии правил он непрерывно в списке. Письма по сделкам — только о найденных позже."""
    since: dict[str, datetime] = {}
    gone: set[str] = set()
    for row in session.scalars(select(RulesVersion).order_by(RulesVersion.id.desc())):
        listed = set(parse_profile(row.profile).notify.recipients)
        for addr in listed - gone:
            since[addr] = row.created_at
        gone |= set(since) - listed
    return since
