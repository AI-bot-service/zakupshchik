"""Карточка закупки → позиции и условия (вид A, JSON). Поля берутся только из белого списка конфига."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.config import DetailsConfig
from app.adapters.extract import ExtractError, get_path
from app.adapters.normalize import parse_money, resolve
from app.core.pii import mask, mask_text


class ItemData(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    qty: Decimal | None
    unit: str | None
    unit_price: Decimal | None
    okpd: str | None


class DealDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    items: list[ItemData]
    # Условия карточки после маскирования ПДн: оплата, основание, доставка, требования
    extra: dict[str, Any]
    files: list[str]


def details_url(cfg: DetailsConfig, context: dict[str, Any]) -> str | None:
    url = resolve(context, cfg.url)
    return url if isinstance(url, str) else None


def _qty(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        qty = Decimal(str(value).replace(",", "."))
    except ArithmeticError:
        return None
    return qty if qty > 0 else None


def _short(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text and text != "-" else None


def parse_details(text: str, cfg: DetailsConfig) -> DealDetails:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractError("карточка закупки не JSON") from exc
    raw_items = get_path(data, cfg.items_path)
    if not isinstance(raw_items, list):
        raise ExtractError(f"в карточке нет позиций по пути «{cfg.items_path}»")
    f = cfg.item_fields
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = _short(resolve(raw, f.name), 2000)
        if not name:
            continue
        items.append(
            ItemData(
                name=mask_text(name),
                qty=_qty(resolve(raw, f.qty)),
                unit=_short(resolve(raw, f.unit), 64),
                unit_price=parse_money(resolve(raw, f.unit_price)),
                okpd=_short(resolve(raw, f.okpd), 32),
            )
        )
    extra = {k: mask(v) for k, spec in cfg.keep.items() if (v := resolve(data, spec)) not in (None, "", "-")}
    files_raw = get_path(data, cfg.files_path) if cfg.files_path else None
    files = [
        mask_text(str(x["name"]))
        for x in (files_raw if isinstance(files_raw, list) else [])
        if isinstance(x, dict) and x.get("name")
    ]
    return DealDetails(items=items, extra=extra, files=files)
