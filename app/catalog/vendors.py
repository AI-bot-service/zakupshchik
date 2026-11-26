"""Словарь вендоров каталога (config/vendors.yaml): узнать вендора по тексту закупки."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Vendor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str
    words: list[str] = Field(min_length=1)
    products: list[str] = Field(default_factory=list)


class VendorsFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    vendors: dict[str, Vendor]


class VendorIndex:
    """Слово ищется как начало слова без учёта регистра; вернёт слаги вендоров и найденные слова."""

    def __init__(self, vendors: dict[str, Vendor]) -> None:
        self.vendors = vendors
        self._patterns = {
            slug: re.compile(
                r"(?<![\w])(" + "|".join(re.escape(w.strip().lower()) for w in (*v.words, *v.products)) + ")",
                re.IGNORECASE,
            )
            for slug, v in vendors.items()
        }

    def find(self, text: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for slug, pattern in self._patterns.items():
            m = pattern.search(text)
            if m:
                found[slug] = m.group(1)
        return found


def load_vendors(path: Path) -> VendorIndex:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return VendorIndex(VendorsFile.model_validate(data).vendors)
