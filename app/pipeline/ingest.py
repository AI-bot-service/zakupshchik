"""Запись площадок и сделок в БД. Повторный прогон обновляет найденное, а не плодит дубли."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import ReturningInsert

from app.adapters.config import BrowserPlatformConfig, PlatformConfig
from app.adapters.normalize import DealData
from app.db.models import AdapterKind, Deal, Platform, PlatformId, PlatformStatus

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
)


@dataclass(frozen=True)
class IngestResult:
    new: int
    updated: int


def sync_platforms(
    session: Session,
    configs: list[PlatformConfig],
    browser: list[BrowserPlatformConfig] | None = None,
) -> dict[str, PlatformId]:
    """Реестр площадок из platforms.yaml в БД. enabled из файла — только для новой площадки:
    дальше галочку ставит менеджер на странице «Площадки», повторный сбор её не сбрасывает."""
    rows_in: list[tuple[str, str, AdapterKind, dict[str, object], bool]] = [
        (c.code, c.name, c.kind.adapter_kind, {**c.model_dump(mode="json"), "access": "free"}, c.enabled)
        for c in configs
    ] + [
        (b.code, b.name, AdapterKind.EXTENSION, {**b.model_dump(mode="json"), "access": "auth"}, b.enabled)
        for b in browser or []
    ]
    for code, name, kind, config, enabled in rows_in:
        stmt = insert(Platform).values(
            code=code,
            name=name,
            adapter_kind=kind,
            config=config,
            status=PlatformStatus.ON if enabled else PlatformStatus.OFF,
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Platform.code],
                set_={
                    "name": stmt.excluded.name,
                    "adapter_kind": stmt.excluded.adapter_kind,
                    "config": stmt.excluded.config,
                    "updated_at": func.now(),
                },
            )
        )
    rows = session.execute(select(Platform.code, Platform.id)).all()
    return {code: PlatformId(pid) for code, pid in rows}


def enabled_codes(session: Session) -> set[str]:
    return set(session.scalars(select(Platform.code).where(Platform.status == PlatformStatus.ON)))


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
        set_={name: base.excluded[name] for name in _REFRESHABLE}
        | {
            # Свежие поля списка + уже загруженная карточка: иначе каждый цикл заново качал бы карточки
            "raw_masked": base.excluded.raw_masked.op("||")(
                func.jsonb_strip_nulls(func.jsonb_build_object("details", Deal.raw_masked["details"]))
            ),
            "updated_at": func.now(),
        },
    ).returning(literal_column("(xmax = 0)"))
    inserted = [bool(flag) for (flag,) in session.execute(upsert).all()]
    new = sum(inserted)
    return IngestResult(new=new, updated=len(inserted) - new)
