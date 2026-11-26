"""Описание площадки из config/platforms.yaml. Только данные: пути, шаблоны, словари — без кода."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.db.models import AdapterKind, Law, Procedure

# Путь в записи, шаблон с {полями} или список вариантов (первый непустой).
FieldSpec = str | list[str]


class ExtractKind(StrEnum):
    JSON = "json"
    NEXT_DATA = "next_data"
    NEXT_RSC = "next_rsc"

    @property
    def adapter_kind(self) -> AdapterKind:
        return AdapterKind.API_JSON if self is not ExtractKind.NEXT_RSC else AdapterKind.HTML


class DealFields(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    external_id: FieldSpec
    url: FieldSpec
    title: FieldSpec
    customer_name: FieldSpec | None = None
    customer_inn: FieldSpec | None = None
    region: FieldSpec | None = None
    nmck: FieldSpec | None = None
    start_at: FieldSpec | None = None
    end_at: FieldSpec | None = None
    procedure_raw: FieldSpec | None = None
    law_raw: FieldSpec | None = None
    smb_flag: FieldSpec | None = None


class PlatformConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str
    kind: ExtractKind
    url: str = Field(pattern=r"^https://")
    enabled: bool = True
    min_interval_s: float = Field(default=4, ge=1)
    queries: list[str] = Field(min_length=1)
    # {query} в строках заменяется словом запроса
    params: dict[str, str | list[str]] = Field(default_factory=dict)
    json_params: dict[str, Any] = Field(default_factory=dict)
    items_path: str = ""
    card_labels: dict[str, str] = Field(default_factory=dict)
    card_suffix_labels: dict[str, str] = Field(default_factory=dict)
    fields: DealFields
    date_formats: list[str] = Field(default_factory=list)
    procedure_map: dict[str, Procedure] = Field(default_factory=dict)
    law_map: dict[str, Law] = Field(default_factory=dict)
    law_default: Law = Law.UNKNOWN
    # Белый список полей записи, которые сохраняются как есть (после маскирования ПДн)
    keep_raw: list[str] = Field(default_factory=list)


class PlatformsFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    platforms: list[PlatformConfig]


def load_platforms(path: Path) -> list[PlatformConfig]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # strict=False только здесь: YAML отдаёт строки там, где ждём StrEnum
    parsed = PlatformsFile.model_validate(data, strict=False)
    codes = [p.code for p in parsed.platforms]
    if len(codes) != len(set(codes)):
        raise ValueError("platforms.yaml: коды площадок повторяются")
    return parsed.platforms
