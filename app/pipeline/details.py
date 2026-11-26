"""Шаг «детали»: для сделок, прошедших префильтр, загрузить позиции и условия из карточки площадки.

После загрузки сделка снова идёт в префильтр (rules_version сброшен): вендор часто виден только в позициях.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.config import PlatformConfig
from app.adapters.details import details_url, parse_details
from app.adapters.extract import ExtractError
from app.adapters.fetch import Fetcher
from app.db.models import Deal, DealItem, DealStatus, Platform


@dataclass
class DetailsRun:
    loaded: int = 0
    errors: list[str] = field(default_factory=list)


def load_details(session: Session, fetcher: Fetcher, configs: list[PlatformConfig], limit: int) -> DetailsRun:
    by_code = {c.code: c for c in configs if c.details is not None}
    run = DetailsRun()
    if not by_code:
        return run
    deals = session.scalars(
        select(Deal)
        .join(Platform)
        .where(
            Deal.status == DealStatus.NEW,
            Platform.code.in_(by_code),
            ~Deal.raw_masked.has_key("details"),
        )
        .order_by(Deal.end_at.asc().nulls_last())
        .limit(limit)
    ).all()
    codes = dict(session.execute(select(Platform.id, Platform.code)).tuples().all())
    for deal in deals:
        cfg = by_code[codes[deal.platform_id]]
        spec = cfg.details
        if spec is None:
            continue
        url = details_url(spec, {"external_id": deal.external_id, **deal.raw_masked})
        if url is None:
            run.errors.append(f"{deal.external_id}: не из чего собрать адрес карточки")
            continue
        try:
            details = parse_details(fetcher.get_url(cfg, url), spec)
        except httpx.HTTPError as exc:
            run.errors.append(f"{deal.external_id}: карточка недоступна ({type(exc).__name__})")
            continue
        except ExtractError as exc:
            run.errors.append(f"{deal.external_id}: {exc}")
            continue
        deal.items = [DealItem(**item.model_dump()) for item in details.items]
        files = [f.model_dump() for f in details.files]
        deal.raw_masked = {**deal.raw_masked, "details": {**details.extra, "files": files}}
        deal.rules_version = None
        run.loaded += 1
    return run
