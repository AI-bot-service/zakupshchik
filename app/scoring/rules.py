"""Профиль правил: схема без выражений и кода. Действующие правила — в БД (вкладка «Админка»),
config/rules.yaml — исходные значения для первой версии. Версия = хэш содержимого и версии логики."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.db.models import Procedure

RULES_FILE = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"
# Поднимать при изменении кода префильтра или оценки: версия правил меняется, сделки пересчитываются
LOGIC_VERSION = b"prefilter-2/score-3"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Filters(_Strict):
    max_nmck_rub: Decimal = Field(gt=0)
    procedures_take: list[Procedure]
    procedures_cut: list[Procedure]
    min_hours_left: float = Field(ge=0)
    smb_only_phrases: list[str]
    software_words: list[str] = Field(min_length=1)
    stop_words: list[str]
    cut_quoted_foreign_product: bool


class Conditions(_Strict):
    max_postpay_days: int = Field(ge=0)
    min_hours_for_kp: float = Field(ge=0)


class Weights(_Strict):
    coverage: float = Field(ge=0, le=1)
    margin: float = Field(ge=0, le=1)
    amount: float = Field(ge=0, le=1)
    payment: float = Field(ge=0, le=1)
    simplicity: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_is_one(self) -> Weights:
        total = self.coverage + self.margin + self.amount + self.payment + self.simplicity
        if abs(total - 1) > 1e-6:
            raise ValueError(f"сумма весов {total:g}, должна быть 1")
        return self


class Notify(_Strict):
    daily_report_at: time
    urgent_hours_left: float = Field(gt=0)
    min_conditions: int = Field(ge=1, le=6)


class Kp(_Strict):
    valid_days: int = Field(ge=1, le=365)
    vat_rate: Decimal = Field(ge=0, le=50)
    default_payment: str = Field(min_length=3, max_length=100)
    default_delivery: str = Field(min_length=3, max_length=100)
    signer_name: str = Field(min_length=3, max_length=100)
    signer_title: str = Field(min_length=3, max_length=100)


class RuleProfile(_Strict):
    filters: Filters
    conditions: Conditions
    weights: Weights
    kp: Kp
    notify: Notify


class Rules:
    """Профиль и его версия; словари слов скомпилированы один раз."""

    def __init__(self, profile: RuleProfile, version: str) -> None:
        self.profile = profile
        self.version = version
        f = profile.filters
        self.smb_only = word_pattern(f.smb_only_phrases)
        self.software = word_pattern(f.software_words)
        self.stop = word_pattern(f.stop_words)


class RulesError(ValueError):
    pass


def word_pattern(words: list[str]) -> re.Pattern[str]:
    """Слово = начало слова в тексте, без учёта регистра; число не продолжается цифрой."""
    parts = [r"(?<!\w)" + re.escape(w.lower()) + (r"(?!\d)" if w[-1:].isdigit() else "") for w in words if w]
    return re.compile("|".join(parts) or r"(?!x)x", re.IGNORECASE)


_RU = {
    "greater_than": "должно быть больше {gt}",
    "greater_than_equal": "должно быть не меньше {ge}",
    "less_than_equal": "должно быть не больше {le}",
    "int_parsing": "нужно целое число",
    "int_from_float": "нужно целое число",
    "float_parsing": "нужно число",
    "decimal_parsing": "нужно число",
    "time_parsing": "нужно время в виде ЧЧ:ММ",
    "string_too_short": "слишком коротко",
    "string_too_long": "слишком длинно",
    "too_short": "нужно хотя бы одно значение",
    "enum": "неизвестное значение",
    "missing": "не заполнено",
}


class RulesFieldError(RulesError):
    """Ошибка в правилах с адресом поля (filters.max_nmck_rub) и текстом по-русски."""

    def __init__(self, source: str, field: str, message: str) -> None:
        super().__init__(f"{source}, поле {field}: {message}")
        self.field = field
        self.message = message


def _field_error(exc: ValidationError, source: str) -> RulesFieldError:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"] if not isinstance(p, int))
    if first["type"] == "value_error":
        message = str(first["msg"]).removeprefix("Value error, ")
    else:
        template = _RU.get(first["type"])
        message = template.format(**first.get("ctx", {})) if template else str(first["msg"])
    return RulesFieldError(source, where, message)


def parse_profile(data: Any, source: str = "правила") -> RuleProfile:  # noqa: ANN401 — сырые данные YAML/JSON
    try:
        profile = RuleProfile.model_validate(data)
    except ValidationError as exc:
        raise _field_error(exc, source) from exc
    overlap = set(profile.filters.procedures_take) & set(profile.filters.procedures_cut)
    if overlap:
        raise RulesError(f"{source}: способ одновременно берём и отсекаем: {sorted(overlap)}")
    return profile


def profile_version(profile: RuleProfile) -> str:
    canonical = json.dumps(profile.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(LOGIC_VERSION + canonical).hexdigest()[:12]


def make_rules(profile: RuleProfile) -> Rules:
    return Rules(profile, profile_version(profile))


def load_rules(path: Path = RULES_FILE) -> Rules:
    """Правила из файла: исходные значения и тесты. В работе — app.scoring.store.active_rules."""
    return make_rules(parse_profile(yaml.safe_load(path.read_bytes()), path.name))
