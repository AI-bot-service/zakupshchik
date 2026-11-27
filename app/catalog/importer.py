"""Импорт выгрузки store.softline.ru (data/store/cards.jsonl, scripts/store_sync.py) в catalog_item.

Повторный запуск обновляет цены и поля по product_id. Запуск: python -m app.catalog.importer [путь]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import CatalogItem

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "store" / "cards.jsonl"
_BATCH = 500


class StoreCard(BaseModel):
    """Карточка магазина в том виде, как её пишет store_sync. Лишнее поле → ошибка (вёрстка поменялась)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    product_id: int
    vendor_slug: str
    sku: str | None
    name: str
    manufacturer: str | None
    price_rub: Decimal | None
    price_on_request: bool | None = None
    min_qty: int | None
    max_qty: int | None
    license_type: str | None
    term: str | None
    version: str | None
    client_type: str | None
    rf_registry_no: str | None
    delivery_days: str | None
    categories: list[str]
    short_specs: list[str]
    specs: dict[str, str]
    fetched_at: datetime


def read_cards(path: Path) -> Iterator[StoreCard | str]:
    """Карточки по одной; битая строка → текст ошибки вместо карточки."""
    with path.open(encoding="utf-8") as fh:
        for no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                yield StoreCard.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                yield f"строка {no}: {type(exc).__name__}"


def _row(card: StoreCard) -> dict[str, object]:
    return {
        "product_id": card.product_id,
        # У части карточек артикула нет — ключом служит номер товара магазина
        "sku": (card.sku or f"store-{card.product_id}")[:128],
        "vendor": card.vendor_slug[:64],
        "manufacturer": card.manufacturer,
        "name": card.name,
        "edition": card.version,
        "license_type": card.license_type,
        "term": card.term,
        "client_type": card.client_type,
        "min_qty": card.min_qty,
        "max_qty": card.max_qty,
        "price": card.price_rub,
        "price_checked_at": card.fetched_at,
        "rf_registry_no": card.rf_registry_no,
        "delivery_days": card.delivery_days,
        "url": card.url,
        "synonyms": [],
    }


def _flush(session: Session, rows: list[dict[str, object]]) -> None:
    stmt = insert(CatalogItem).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[CatalogItem.product_id],
            set_={k: stmt.excluded[k] for k in rows[0] if k != "product_id"} | {"updated_at": func.now()},
        )
    )


def import_cards(session: Session, path: Path) -> tuple[int, list[str]]:
    # Последняя версия карточки в файле побеждает (выгрузка дописывает перепроверки в конец)
    latest: dict[int, dict[str, object]] = {}
    errors: list[str] = []
    for item in read_cards(path):
        if isinstance(item, str):
            errors.append(item)
        else:
            latest[item.product_id] = _row(item)
    rows = list(latest.values())
    for i in range(0, len(rows), _BATCH):
        _flush(session, rows[i : i + _BATCH])
    return len(rows), errors


def main(argv: list[str]) -> int:
    from app.core.settings import Settings
    from app.db.session import make_engine, make_session_factory

    path = Path(argv[0]) if argv else DEFAULT_PATH
    factory = make_session_factory(make_engine(Settings().database_url.get_secret_value().encoded_string()))
    with factory.begin() as session:
        count, errors = import_cards(session, path)
    print(f"каталог: загружено {count} позиций, ошибок разбора {len(errors)}")
    for e in errors[:10]:
        print(f"  ! {e}")
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
