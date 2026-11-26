"""Журнал шагов агента: каждая запись — своя транзакция, чтобы токены и ₽ не терялись при сбое разбора."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AgentStep


class Journal:
    def __init__(self, factory: sessionmaker[Session] | None, run_id: str, deal_id: int | None) -> None:
        self._factory = factory
        self.run_id = run_id
        self.deal_id = deal_id
        # Копия для тестов и итогов разбора: строки ORM после коммита отсоединены
        self.steps: list[dict[str, Any]] = []

    def add(
        self,
        step: str,
        output: dict[str, Any] | None = None,
        model: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_rub: Decimal = Decimal(0),
        ms: int = 0,
        input_hash: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        row = AgentStep(
            run_id=self.run_id,
            deal_id=self.deal_id,
            step=step[:64],
            input_hash=input_hash,
            output=output or {},
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_rub=cost_rub,
            ms=ms,
            error=error,
        )
        self.steps.append({"step": step, "output": output or {}, "cost_rub": cost_rub})
        if self._factory is not None:
            with self._factory.begin() as session:
                session.add(row)
