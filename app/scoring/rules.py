"""Профиль правил из config/rules.yaml: схема без выражений и кода, версия = хэш файла."""

from __future__ import annotations

import hashlib
import re
from datetime import time
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.db.models import Procedure

RULES_FILE = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"
# Поднимать при изменении кода префильтра или оценки: версия правил меняется, сделки пересчитываются
LOGIC_VERSION = b"prefilter-2/score-1"


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


class RuleProfile(_Strict):
    filters: Filters
    conditions: Conditions
    weights: Weights
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


def load_rules(path: Path = RULES_FILE) -> Rules:
    raw = path.read_bytes()
    try:
        profile = RuleProfile.model_validate(yaml.safe_load(raw))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise RulesError(f"rules.yaml, поле {where}: {first['msg']}") from exc
    overlap = set(profile.filters.procedures_take) & set(profile.filters.procedures_cut)
    if overlap:
        raise RulesError(f"rules.yaml: способ одновременно берём и отсекаем: {sorted(overlap)}")
    return Rules(profile, hashlib.sha256(LOGIC_VERSION + raw).hexdigest()[:12])
