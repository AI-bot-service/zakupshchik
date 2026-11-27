"""Что агент сдаёт после разбора закупки. Схема уходит модели параметрами submit_analysis, проверяет код."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import ContractFormat, MatchKind, PaymentTerms


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConstraintKind(StrEnum):
    DOMESTIC_ONLY = "domestic_only"
    NO_EQUIVALENT = "no_equivalent"
    PARTICIPANT_LICENSE = "participant_license"
    SMB_ONLY = "smb_only"
    GUARANTEE = "guarantee"
    OTHER = "other"


class Position(_Strict):
    name: str = Field(min_length=2, max_length=500, description="Позиция своими словами закупки, кратко")
    quote: str = Field(min_length=3, max_length=400, description="Дословный фрагмент закупки или файла")
    qty: float | None = Field(
        gt=0, description="Количество лицензий/устройств/пользователей; нет в тексте — null"
    )
    unit: str | None = Field(max_length=64, description="Единица: лицензия, устройство, пользователь, шт")
    term_months: int | None = Field(ge=1, le=120, description="Срок лицензии в месяцах; нет — null")
    vendor: str | None = Field(max_length=100, description="Вендор из текста или по продукту; неясно — null")
    product: str | None = Field(max_length=200, description="Продукт и редакция, как поняли")
    match: MatchKind = Field(description="exact — тот же продукт и редакция; analog — та же функция; none")
    catalog_item_id: int | None = Field(description="id из ответа search_catalog; для none — null")
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(
        min_length=3, max_length=600, description="Почему такое сопоставление, простыми словами"
    )


class Constraint(_Strict):
    kind: ConstraintKind = Field(
        description=(
            "domestic_only — только отечественное ПО/реестр/нацрежим; "
            "no_equivalent — эквивалент не допускается; "
            "participant_license — лицензия ФСТЭК/ФСБ или статус партнёра вендора у участника; "
            "smb_only — только МСП; "
            "guarantee — обеспечение заявки, исполнения контракта или гарантийных обязательств "
            "(деньги или банковская гарантия от участника); гарантийный срок и гарантия поставщика "
            "на ПО — не guarantee, а other; "
            "other — иное важное требование"
        )
    )
    quote: str = Field(min_length=3, max_length=400, description="Дословный фрагмент")
    note: str = Field(min_length=3, max_length=400, description="Что это значит для поставщика")


class Analysis(_Strict):
    subject: str = Field(min_length=3, max_length=300, description="Что покупают, одной фразой")
    is_license_supply: bool = Field(
        description="true — поставка лицензий/прав на готовое ПО; услуги, разработка — false"
    )
    not_license_reason: str | None = Field(max_length=400, description="Если false — почему")
    positions: list[Position] = Field(max_length=30)
    constraints: list[Constraint] = Field(default_factory=list, max_length=15)
    payment_terms: PaymentTerms
    payment_days: int | None = Field(ge=0, le=365, description="Срок оплаты в днях после поставки/подписания")
    payment_quote: str | None = Field(max_length=400, description="Дословный фрагмент про оплату")
    contract_format: ContractFormat = Field(
        description=(
            "none — по счёту; supplier_template — договор поставщика; customer_template — проект заказчика"
        )
    )
    delivery_days: int | None = Field(ge=0, le=365, description="Срок поставки, дней")
    missing: list[str] = Field(
        default_factory=list, max_length=10, description="Чего не хватает для КП, коротко"
    )


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """$ref → определение: не все модели за шлюзом понимают ссылки в параметрах инструмента."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:  # noqa: ANN401 — обход произвольной JSON-схемы
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


def analysis_parameters() -> dict[str, Any]:
    return _inline_refs(Analysis.model_json_schema())
