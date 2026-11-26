"""Запись площадок и сделок в БД. Повторный прогон обновляет найденное, а не плодит дубли."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import ReturningInsert

from app.adapters.config import PlatformConfig
from app.adapters.normalize import DealData
from app.db.models import Deal, Platform, PlatformId, PlatformStatus

# Поля, которые площадка может поменять после публикации (продлили срок, уточнили НМЦ).
# Статус, оценку и решения менеджера повторный сбор не трогает.
_REFRESHABLE = (
    "url",
    "title",
    "law",
    "procedure",
    "procedure_raw",
    "customer_name",
    "customer_inn",
    "region",
    "nmck",
    "start_at",
    "end_at",
    "raw_masked",
)


@dataclass(frozen=True)
class IngestResult:
    new: int
    updated: int


def sync_platforms(session: Session, configs: list[PlatformConfig]) -> dict[str, PlatformId]:
    for cfg in configs:
        stmt = insert(Platform).values(
            code=cfg.code,
            name=cfg.name,
            adapter_kind=cfg.kind.adapter_kind,
            config=cfg.model_dump(mode="json"),
            status=PlatformStatus.ON if cfg.enabled else PlatformStatus.OFF,
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Platform.code],
                set_={
                    "name": stmt.excluded.name,
                    "adapter_kind": stmt.excluded.adapter_kind,
                    "config": stmt.excluded.config,
                    "status": stmt.excluded.status,
                    "updated_at": func.now(),
                },
            )
        )
    rows = session.execute(select(Platform.code, Platform.id)).all()
    return {code: PlatformId(pid) for code, pid in rows}


def mark_platform(session: Session, platform_id: PlatformId, error: str | None) -> None:
    platform = session.get_one(Platform, platform_id)
    if error is None:
        platform.last_ok_at = datetime.now(UTC)
        platform.last_error = None
    else:
        platform.last_error = error[:2000]


def save_deals(session: Session, platform_id: PlatformId, deals: list[DealData]) -> IngestResult:
    if not deals:
        return IngestResult(new=0, updated=0)
    values = [{"platform_id": platform_id, **d.model_dump(exclude={"platform_code"})} for d in deals]
    base = insert(Deal).values(values)
    # xmax = 0 у строки, которую вставили, а не обновили
    upsert: ReturningInsert[tuple[bool]] = base.on_conflict_do_update(
        index_elements=[Deal.platform_id, Deal.external_id],
        set_={name: base.excluded[name] for name in _REFRESHABLE} | {"updated_at": func.now()},
    ).returning(literal_column("(xmax = 0)"))
    inserted = [bool(flag) for (flag,) in session.execute(upsert).all()]
    new = sum(inserted)
    return IngestResult(new=new, updated=len(inserted) - new)
