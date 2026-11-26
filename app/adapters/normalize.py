"""Запись площадки → единая сделка DealData. Нет значения в источнике → None, ничего не додумываем."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.config import FieldSpec, PlatformConfig
from app.adapters.extract import Record, get_path
from app.core.pii import mask, mask_text
from app.db.models import Law, Procedure, TriState

MSK = timezone(timedelta(hours=3), "MSK")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_MONEY = re.compile(r"\d[\d\s ]*(?:[.,]\d{1,2})?")


class DealData(BaseModel):
    """Одна сделка любой площадки после нормализации — вход для дедупа и префильтра."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    platform_code: str
    external_id: str = Field(min_length=1, max_length=128)
    url: str = Field(pattern=r"^https://")
    title: str = Field(min_length=1)
    law: Law
    procedure: Procedure
    procedure_raw: str | None
    customer_name: str | None
    customer_inn: str | None
    region: str | None
    nmck: Decimal | None
    start_at: datetime | None
    end_at: datetime | None
    smb_only: TriState
    smb_evidence: str | None
    raw_masked: dict[str, Any]


class NormalizeError(ValueError):
    pass


def _fill_template(template: str, values: dict[str, Any]) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)


def resolve(record: Record, spec: FieldSpec | None) -> Any:
    """Путь, шаблон или список вариантов → первое непустое значение."""
    if spec is None:
        return None
    for variant in [spec] if isinstance(spec, str) else spec:
        if "{" in variant:
            values = {name: get_path(record, name) for name in _PLACEHOLDER.findall(variant)}
            if any(v in (None, "") for v in values.values()):
                continue
            value: Any = _fill_template(variant, values)
        else:
            value = get_path(record, variant)
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def parse_money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    m = _MONEY.search(str(value))
    if not m:
        return None  # «НМЦ не установлена» и т.п.
    digits = re.sub(r"[\s ]", "", m.group(0)).replace(",", ".")
    try:
        return Decimal(digits).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def parse_dt(value: Any, formats: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in formats:
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    # Площадки РФ без пояса пишут московское время
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MSK)


def _inn(value: Any) -> str | None:
    text = _text(value)
    # ИНН физлица (12 цифр) — ПДн, не храним
    return text if text and re.fullmatch(r"\d{10}", text) else None


def _lookup[T](mapping: dict[str, T], raw: str | None) -> T | None:
    if raw is None:
        return None
    if raw in mapping:
        return mapping[raw]
    low = raw.lower()
    return next((v for k, v in mapping.items() if k.lower() == low), None)


def normalize(record: Record, cfg: PlatformConfig) -> DealData:
    f = cfg.fields
    external_id = _text(resolve(record, f.external_id))
    url = _text(resolve(record, f.url))
    title = _text(resolve(record, f.title))
    if not external_id or not url or not title:
        raise NormalizeError(f"{cfg.code}: нет номера, ссылки или названия закупки")

    procedure_raw = _text(resolve(record, f.procedure_raw))
    law_raw = _text(resolve(record, f.law_raw))
    smb_flag = resolve(record, f.smb_flag)
    smb = TriState.UNKNOWN
    if isinstance(smb_flag, bool):
        smb = TriState.YES if smb_flag else TriState.NO

    return DealData(
        platform_code=cfg.code,
        external_id=external_id,
        url=url,
        title=mask_text(title),
        law=_lookup(cfg.law_map, law_raw) or cfg.law_default,
        procedure=_lookup(cfg.procedure_map, procedure_raw) or Procedure.UNKNOWN,
        procedure_raw=procedure_raw,
        customer_name=_mask_opt(_text(resolve(record, f.customer_name))),
        customer_inn=_inn(resolve(record, f.customer_inn)),
        region=_text(resolve(record, f.region)),
        nmck=parse_money(resolve(record, f.nmck)),
        start_at=parse_dt(resolve(record, f.start_at), cfg.date_formats),
        end_at=parse_dt(resolve(record, f.end_at), cfg.date_formats),
        smb_only=smb,
        smb_evidence="флаг площадки «только МСП»" if smb is TriState.YES else None,
        raw_masked={k: mask(record[k]) for k in cfg.keep_raw if k in record},
    )


def _mask_opt(text: str | None) -> str | None:
    return mask_text(text) if text else None
