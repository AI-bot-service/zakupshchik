"""Адаптеры площадок: один контракт для всех — описание площадки → список DealData (ARCHITECTURE.md §3.1)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.adapters.config import PlatformConfig
from app.adapters.extract import ExtractError, extract
from app.adapters.fetch import Fetcher
from app.adapters.normalize import DealData, NormalizeError, normalize


@dataclass
class CollectResult:
    deals: list[DealData] = field(default_factory=list)
    # Ошибки — по запросу: «что случилось», без тел ответов (там могут быть ПДн)
    errors: list[str] = field(default_factory=list)
    skipped: int = 0
    # Поиск площадок отдаёт и давно закрытые закупки — их не сохраняем
    closed: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.deals) or not self.errors


def parse(text: str, cfg: PlatformConfig) -> CollectResult:
    result = CollectResult()
    for record in extract(text, cfg):
        try:
            result.deals.append(normalize(record, cfg))
        except (NormalizeError, ValueError):
            result.skipped += 1
    return result


def collect(
    cfg: PlatformConfig, fetcher: Fetcher, queries: Sequence[str], now: datetime | None = None
) -> CollectResult:
    """Слова поиска из правил на одной площадке; сделки уникальны по external_id, закрытые отброшены."""
    now = now or datetime.now(UTC)
    total = CollectResult()
    seen: set[str] = set()
    for i, query in enumerate(queries):
        try:
            part = parse(fetcher.get(cfg, query), cfg)
        except httpx.HTTPStatusError as exc:
            total.errors.append(f"«{query}»: площадка ответила {exc.response.status_code}")
            continue
        except httpx.TransportError as exc:
            # Площадка недоступна (сеть, блок по IP): после повторов не ждём таймаутов на остальных словах
            rest = len(queries) - i - 1
            note = f", ещё {rest} запр. отложено до следующего цикла" if rest else ""
            total.errors.append(f"«{query}»: нет соединения ({type(exc).__name__}){note}")
            break
        except ExtractError as exc:
            total.errors.append(f"«{query}»: {exc}")
            continue
        total.skipped += part.skipped
        for deal in part.deals:
            if deal.end_at is not None and deal.end_at <= now:
                total.closed += 1
            elif deal.external_id not in seen:
                seen.add(deal.external_id)
                total.deals.append(deal)
    return total
