"""Публичное состояние стенда для жюри и проверки живучести. Без данных сделок."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from app.api.auth import current_user
from app.core.provenance import BUILD_MARK
from app.db.session import db_alive

router = APIRouter()


class Health(StrEnum):
    OK = "ok"
    DOWN = "down"


class ComponentState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str
    state: Health
    note_ru: str


class StatusReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    build: str
    version: str
    checked_at: datetime
    components: list[ComponentState]

    @property
    def ok(self) -> bool:
        return all(c.state is Health.OK for c in self.components)


def collect_status(request: Request) -> StatusReport:
    db_ok = db_alive(request.app.state.engine)
    return StatusReport(
        build=BUILD_MARK,
        version=request.app.version,
        checked_at=datetime.now(UTC),
        components=[
            ComponentState(name="Приложение", state=Health.OK, note_ru="отвечает"),
            ComponentState(
                name="База данных",
                state=Health.OK if db_ok else Health.DOWN,
                note_ru="подключена" if db_ok else "нет соединения, сделки не сохраняются",
            ),
        ],
    )


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/status")
def status_json(report: Annotated[StatusReport, Depends(collect_status)]) -> StatusReport:
    return report


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request, report: Annotated[StatusReport, Depends(collect_status)]) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request, "status.html", {"report": report, "user": current_user(request)}
    )


STAMP_RULES = (
    ("wait", "Ещё разбираем", "робот", "Закупка прошла отбор, агент ещё читает документы."),
    ("take", "Стоит взять", "робот", "Выполнены все 6 условий."),
    ("decide", "Нужно ваше решение", "робот", "5 из 6, а если позиций нет в каталоге — от 4."),
    ("skip", "Не берём", "робот", "4 и меньше из 6. Сделка остаётся во «Всех разобранных», её можно взять."),
    (
        "filtered",
        "Отсеяна",
        "робот",
        "Не прошла отбор по правилам: способ, сумма, стоп-слова. До разбора не дошла.",
    ),
    ("work", "В работе", "менеджер", "«Беру в работу»: робот собирает КП, напоминания прекращаются."),
    ("sent", "Отправлено", "менеджер", "«КП отправлено»: КП ушло заказчику, на штампе дата."),
    ("declined", "Отказ", "менеджер", "«Не беру» с причиной, она сохраняется в журнале сделки."),
    ("late", "Срок вышел", "робот", "Сделка была в работе, но приём закрылся без отметки «КП отправлено»."),
    ("closed", "Приём закрыт", "робот", "Срок подачи прошёл, решения по сделке не было."),
)


@router.get("/design", response_class=HTMLResponse)
def design_page(request: Request) -> HTMLResponse:
    """Дизайн-схема стенда: токены, компоненты, все штампы и как сделка переходит между ними."""
    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request, "design.html", {"stamps": STAMP_RULES, "user": current_user(request)}
    )
