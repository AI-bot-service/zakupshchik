"""Ручной запуск цикла со стенда и сводка журнала прогонов.

Приложение и воркер — разные контейнеры, общая у них только БД. Кнопка «Запустить цикл» пишет в журнал шаг
`manual_run` в состоянии pending; воркер раз в несколько секунд забирает его (SKIP LOCKED), проводит сбор и
разбор вне расписания и записывает итог в тот же шаг.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AgentStep

MANUAL_STEP = "manual_run"
# Заявка, которую воркер не закрыл за это время (упал, перезапущен), не блокирует новую
STALE_AFTER = timedelta(minutes=30)


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


STATE_RU = {
    RunState.PENDING: "ждёт воркер",
    RunState.RUNNING: "идёт",
    RunState.DONE: "готово",
    RunState.FAILED: "сбой",
}


def _active(session: Session, now: datetime) -> AgentStep | None:
    row = session.scalars(
        select(AgentStep).where(AgentStep.step == MANUAL_STEP).order_by(AgentStep.id.desc()).limit(1)
    ).first()
    if row is None or row.output.get("state") not in (RunState.PENDING, RunState.RUNNING):
        return None
    return row if now - row.created_at < STALE_AFTER else None


def request_run(session: Session, user: str, now: datetime) -> tuple[AgentStep, bool]:
    """Заявка на внеочередной цикл. Если уже есть незакрытая — возвращает её (повторное нажатие не плодит)."""
    active = _active(session, now)
    if active is not None:
        return active, False
    row = AgentStep(
        run_id=str(uuid.uuid4()),
        step=MANUAL_STEP,
        output={"state": RunState.PENDING.value, "by": user[:255]},
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row, True


def take_pending(session: Session) -> AgentStep | None:
    """Воркер: забрать одну заявку. Строка блокируется до конца транзакции, второй воркер её пропустит."""
    row = session.scalars(
        select(AgentStep)
        .where(AgentStep.step == MANUAL_STEP, AgentStep.output["state"].astext == RunState.PENDING.value)
        .order_by(AgentStep.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if row is None:
        return None
    row.output = {**row.output, "state": RunState.RUNNING.value, "started": datetime.now(UTC).isoformat()}
    return row


def finish(session: Session, row_id: int, summary: dict[str, Any], ms: int, failed: bool) -> None:
    row = session.get(AgentStep, row_id)
    if row is None:
        return
    state = RunState.FAILED if failed else RunState.DONE
    row.output = {**row.output, **summary, "state": state.value, "finished": datetime.now(UTC).isoformat()}
    row.ms = ms
    if failed:
        row.error = {"message": str(summary.get("error", ""))[:500]}


@dataclass(slots=True)
class PlatformLine:
    code: str
    found: int
    new: int
    closed: int
    error: str


@dataclass(slots=True)
class RunLine:
    run_id: str
    at: datetime
    kind: str  # collect | analyze | manual
    platforms: list[PlatformLine] = field(default_factory=list)
    passed: int = 0
    filtered_out: int = 0
    details: int = 0
    deals: int = 0
    llm_calls: int = 0
    tokens: int = 0
    cost_rub: Decimal = Decimal(0)
    errors: int = 0
    ms: int = 0
    state: RunState | None = None
    quiet: bool = False  # сбор без нового: ни новых закупок, ни прошедших отбор, ни ошибок
    by: str = ""
    summary: dict[str, Any] = field(default_factory=dict)


def recent_runs(session: Session, limit: int = 20) -> list[RunLine]:
    """Последние прогоны: сбор (площадки, префильтр), разбор агентом (сделки, вызовы, ₽) и ручные запуски."""
    last = func.max(AgentStep.id).label("last")
    ids = [
        r.run_id
        for r in session.execute(
            select(AgentStep.run_id, last).group_by(AgentStep.run_id).order_by(last.desc()).limit(limit)
        )
    ]
    if not ids:
        return []
    steps = session.scalars(select(AgentStep).where(AgentStep.run_id.in_(ids)).order_by(AgentStep.id)).all()
    by_run: dict[str, list[AgentStep]] = {}
    for s in steps:
        by_run.setdefault(s.run_id, []).append(s)
    return [_line(run_id, by_run[run_id]) for run_id in ids if run_id in by_run]


def _line(run_id: str, steps: list[AgentStep]) -> RunLine:
    first = steps[0]
    if first.step == MANUAL_STEP:
        out = first.output
        return RunLine(
            run_id=run_id,
            at=first.created_at,
            kind="manual",
            state=RunState(out.get("state", RunState.PENDING.value)),
            by=str(out.get("by", "")),
            summary={k: v for k, v in out.items() if k not in ("state", "by", "started", "finished")},
            ms=first.ms,
            errors=1 if first.error else 0,
        )
    kind = "collect" if any(s.step == "collect" for s in steps) else "analyze"
    line = RunLine(run_id=run_id, at=first.created_at, kind=kind)
    deals: set[int] = set()
    for s in steps:
        out = s.output or {}
        line.cost_rub += s.cost_rub or Decimal(0)
        line.tokens += (s.tokens_in or 0) + (s.tokens_out or 0)
        if s.error:
            line.errors += 1
        if s.deal_id is not None:
            deals.add(s.deal_id)
        if s.model:
            line.llm_calls += 1
        if s.step == "collect":
            errs = out.get("errors") or []
            line.platforms.append(
                PlatformLine(
                    code=str(out.get("platform", "")),
                    found=int(out.get("found", 0)),
                    new=int(out.get("new", 0)),
                    closed=int(out.get("closed", 0)),
                    error="; ".join(str(e) for e in errs)[:300],
                )
            )
            line.errors += 1 if errs else 0
        elif s.step == "prefilter":
            line.passed += int(out.get("passed", 0))
            line.filtered_out += int(out.get("filtered_out", 0))
        elif s.step == "details":
            line.details += int(out.get("loaded", 0))
    line.deals = len(deals)
    line.quiet = kind == "collect" and not (
        line.errors or line.passed or line.filtered_out or line.details or any(p.new for p in line.platforms)
    )
    span = steps[-1].created_at - first.created_at
    line.ms = int(span.total_seconds() * 1000) + steps[-1].ms
    return line


def active_manual(session: Session, now: datetime) -> AgentStep | None:
    return _active(session, now)
