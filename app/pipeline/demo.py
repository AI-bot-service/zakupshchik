"""Демо-показ: повтор реальной закупки как новой сделки в назначенное время.

Заказчица разрешила показывать работу на ретро-данных (чат задачи, 27.11). Повтор — копия закупки, уже
найденной роботом на площадке: те же карточка, позиции и документы, новые «размещена» и номер с хвостом
`~demo…`. Дальше её ведёт обычный конвейер: префильтр → разбор агентом → «N из 6» → КП → письмо и
уведомление плагина. Готовый разбор не копируется — модель работает вживую.

Заявка = шаг журнала `demo_drop` (pending, время выпуска); воркер раз в 5 с выпускает созревшие.
Повтор помечен `raw_masked.demo` — стенд, плагин и письмо подписывают его «демо-повтор».
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import AgentStep, Deal, DealFile, DealItem, DealStatus, Platform
from app.pipeline.attachments import FILES_DIR, deal_dir
from app.pipeline.runs import RunState
from app.pipeline.status_log import change_status, robot

DEMO_STEP = "demo_drop"
DEMO_MARK = "demo"
# Если у исходной закупки приём уже закрыт — столько же времени на подачу, сколько было у оригинала
MIN_WINDOW = timedelta(hours=24)
WHO = robot("демо-показ")

_COPIED = (
    "url",
    "title",
    "law",
    "procedure",
    "procedure_raw",
    "customer_inn",
    "customer_name",
    "region",
    "nmck",
    "kp_min_valid_days",
    "payment_terms",
    "payment_days",
    "contract_format",
    "has_tz",
    "smb_only",
    "smb_evidence",
)


class DemoError(ValueError):
    pass


def is_demo(deal: Deal) -> bool:
    return DEMO_MARK in deal.raw_masked


def schedule(session: Session, user: str, source_id: int, at: datetime, now: datetime) -> AgentStep:
    source = session.get(Deal, source_id)
    if source is None:
        raise DemoError(f"сделки №{source_id} нет")
    if is_demo(source):
        raise DemoError("это уже демо-повтор — укажите исходную сделку")
    row = AgentStep(
        run_id=str(uuid.uuid4()),
        step=DEMO_STEP,
        output={
            "state": RunState.PENDING.value,
            "by": user[:255],
            "source": source_id,
            "at": max(at, now).isoformat(),
        },
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def pending(session: Session) -> list[AgentStep]:
    return list(
        session.scalars(
            select(AgentStep)
            .where(AgentStep.step == DEMO_STEP, AgentStep.output["state"].astext == RunState.PENDING.value)
            .order_by(AgentStep.output["at"].astext)
        )
    )


def recent(session: Session, limit: int = 5) -> list[AgentStep]:
    return list(
        session.scalars(
            select(AgentStep).where(AgentStep.step == DEMO_STEP).order_by(AgentStep.id.desc()).limit(limit)
        )
    )


def cancel(session: Session, row_id: int) -> bool:
    row = session.get(AgentStep, row_id)
    if row is None or row.step != DEMO_STEP or row.output.get("state") != RunState.PENDING.value:
        return False
    row.output = {**row.output, "state": RunState.FAILED.value, "error": "отменено"}
    return True


def take_due(session: Session, now: datetime) -> AgentStep | None:
    """Воркер: одна созревшая заявка, строка заблокирована до конца транзакции."""
    row = session.scalars(
        select(AgentStep)
        .where(
            AgentStep.step == DEMO_STEP,
            AgentStep.output["state"].astext == RunState.PENDING.value,
            AgentStep.output["at"].astext <= now.isoformat(),
        )
        .order_by(AgentStep.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if row is not None:
        row.output = {**row.output, "state": RunState.RUNNING.value, "started": now.isoformat()}
    return row


def release(session: Session, source_id: int, now: datetime, root: Path = FILES_DIR) -> Deal:
    """Копия закупки как новая сделка (статус NEW, без разбора) + её документы с диска."""
    src = session.get(Deal, source_id)
    if src is None:
        raise DemoError(f"сделки №{source_id} нет")
    code = session.get_one(Platform, src.platform_id).code
    window = (src.end_at - src.start_at) if src.end_at and src.start_at else MIN_WINDOW
    end_at = (
        src.end_at if src.end_at and src.end_at > now + timedelta(hours=1) else now + max(window, MIN_WINDOW)
    )
    raw = {k: v for k, v in src.raw_masked.items() if k != DEMO_MARK}
    raw[DEMO_MARK] = {
        "source_id": src.id,
        "source_external_id": src.external_id,
        "source_published": src.start_at.isoformat() if src.start_at else None,
        "released_at": now.isoformat(),
    }
    deal = Deal(
        platform_id=src.platform_id,
        external_id=f"{src.external_id}~demo{now:%m%d%H%M%S}",
        start_at=now,
        end_at=end_at,
        raw_masked=raw,
        status=DealStatus.NEW,
        score_breakdown={},
        **{k: getattr(src, k) for k in _COPIED},
    )
    deal.items = [
        DealItem(name=i.name, okpd=i.okpd, qty=i.qty, unit=i.unit, unit_price=i.unit_price) for i in src.items
    ]
    session.add(deal)
    session.flush()
    _copy_files(session, src, deal, code, root)
    change_status(
        session,
        deal,
        DealStatus.NEW,
        WHO,
        f"демо-повтор закупки {src.external_id} (ретро-данные)",
        always=True,
    )
    return deal


def _copy_files(session: Session, src: Deal, deal: Deal, code: str, root: Path) -> None:
    """Документы уже скачаны для оригинала — копируем с текстом, площадку повторно не дёргаем."""
    target = deal_dir(root, code, deal.external_id)
    for f in session.scalars(select(DealFile).where(DealFile.deal_id == src.id)):
        path = Path(f.path)
        if not path.exists() or path.name.startswith("КП_"):
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / path.name)
        txt = path.with_name(path.name + ".txt")
        if txt.exists():
            shutil.copy2(txt, target / txt.name)
        session.add(
            DealFile(
                deal_id=deal.id,
                name=f.name,
                mime=f.mime,
                size=f.size,
                sha256=f.sha256,
                source_url=f.source_url,
                path=str(target / path.name),
            )
        )
    session.flush()


def finish(session: Session, row_id: int, deal_id: int | None, error: str | None) -> None:
    row = session.get(AgentStep, row_id)
    if row is None:
        return
    state = RunState.FAILED if error else RunState.DONE
    row.output = {
        **row.output,
        "state": state.value,
        "deal_id": deal_id,
        "finished": datetime.now(UTC).isoformat(),
        **({"error": error[:500]} if error else {}),
    }
    row.deal_id = deal_id


def purge(session: Session, root: Path = FILES_DIR) -> int:
    """Убрать все демо-повторы со стенда (после записи видео). Оригиналы не трогаем."""
    deals = list(session.scalars(select(Deal).where(Deal.raw_masked.has_key(DEMO_MARK))))
    codes = dict(session.execute(select(Platform.id, Platform.code)).tuples().all())
    for d in deals:
        shutil.rmtree(deal_dir(root, codes[d.platform_id], d.external_id), ignore_errors=True)
    ids = [d.id for d in deals]
    if ids:
        session.execute(delete(Deal).where(Deal.id.in_(ids)))
    return len(ids)
