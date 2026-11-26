"""Поиск по каталогу: вендор по словарю → похожие позиции по триграммам, артикул — точно.

Из запроса убираются общие слова закупок («поставка неисключительных прав на использование ПО»), иначе
продукт тонет в канцелярите. Без вендора и без отличительных слов не ищем: «Программное обеспечение» совпадёт
с чем угодно, честнее вернуть пусто и разбирать вложение.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Float, Select, bindparam, cast, func, or_, select
from sqlalchemy.orm import Session

from app.catalog.vendors import VendorIndex
from app.db.models import CatalogItem

_SKU = re.compile(r"\b[A-Z]{2,}[A-Z0-9-]{4,}\b")
_GENERIC = re.compile(
    r"(?i)(?<!\w)(?:оказани\w*|услуг\w*|поставк\w*|передач\w*|предоставлени\w*|продлени\w*|приобретени\w*"
    r"|неисключительн\w*|пользовательск\w*|простой|права?|право|лицензи\w*|лицензионн\w*|использовани\w*"
    r"|использовать|программ\w*|обеспечени\w*|эвм|условиях|согласно|спецификаци\w*|приложени\w*"
    r"|заказчик\w*|компьютерн\w*|сертифицированн\w*|россии|сертификат\w*|для|на|по|с|и|в|№)(?!\w)"
)
_MIN_DISTINCT = 4


class CatalogHit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: int
    vendor: str
    sku: str
    name: str
    price: Decimal | None
    url: str
    similarity: float


def distinct_part(text: str) -> str:
    """То, что отличает продукт: без общих слов, кавычек и скобок."""
    cleaned = _GENERIC.sub(" ", re.sub(r"[«»\"()\[\],;:]", " ", text))
    return " ".join(cleaned.split())


def _select(query: str) -> Select[CatalogItem, float]:
    sim = cast(func.word_similarity(bindparam("q", query), CatalogItem.name), Float).label("sim")
    return select(CatalogItem, sim).order_by(sim.desc(), CatalogItem.price.is_(None), CatalogItem.price)


def search_catalog(
    session: Session,
    vendors: VendorIndex,
    query: str,
    context: str = "",
    limit: int = 5,
    min_similarity: float = 0.35,
) -> list[CatalogHit]:
    """query — позиция закупки; context — название сделки (там часто назван вендор)."""
    found = vendors.find(query) or vendors.find(context)
    skus = _SKU.findall(query)
    distinct = distinct_part(query)
    if not skus and (len(distinct) < _MIN_DISTINCT or (not found and len(distinct.split()) < 2)):
        return []
    stmt = _select(distinct or query)
    if skus:
        stmt = stmt.where(or_(CatalogItem.sku.in_(skus), CatalogItem.vendor.in_(found or ["-"])))
    elif found:
        stmt = stmt.where(CatalogItem.vendor.in_(found))
    else:
        stmt = stmt.where(bindparam("q2", distinct).op("<%")(CatalogItem.name))
    hits: dict[str, CatalogHit] = {}
    for item, sim in session.execute(stmt.limit(limit * 4)).all():
        score = 1.0 if item.sku in skus else round(float(sim), 3)
        # Одинаковые названия с разными артикулами (тиражи) — показываем одно
        if score >= min_similarity and item.name not in hits:
            hits[item.name] = CatalogHit(
                id=item.id,
                vendor=item.vendor,
                sku=item.sku,
                name=item.name,
                price=item.price,
                url=item.url,
                similarity=score,
            )
    return sorted(hits.values(), key=lambda h: h.similarity, reverse=True)[:limit]
