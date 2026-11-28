"""Цикл сбора: все включённые площадки → сделки в БД, по шагу журнала на площадку.

Ручной запуск: python -m app.collector [код_площадки ...]
"""

from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.adapters import collect
from app.adapters.config import PlatformConfig, load_platforms
from app.adapters.fetch import Fetcher
from app.catalog.vendors import VendorIndex, load_vendors
from app.db.models import AgentStep
from app.pipeline.details import load_details
from app.pipeline.ingest import enabled_codes, mark_platform, save_deals, sync_platforms
from app.pipeline.prefilter import run_prefilter
from app.scoring.rules import Rules
from app.scoring.store import active_rules

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PLATFORMS_FILE = CONFIG_DIR / "platforms.yaml"
VENDORS_FILE = CONFIG_DIR / "vendors.yaml"
log = structlog.get_logger("collector")
# Карточек за цикл: при 3 с на запрос — до 2 минут
DETAILS_PER_CYCLE = 40


@dataclass(frozen=True)
class PlatformRun:
    code: str
    found: int
    new: int
    updated: int
    skipped: int
    closed: int
    errors: list[str]
    ms: int


def run_cycle(
    session_factory: sessionmaker[Session],
    fetcher: Fetcher,
    configs: list[PlatformConfig],
    rules: Rules,
    vendors: VendorIndex,
    only: set[str] | None = None,
) -> tuple[list[PlatformRun], dict[str, int]]:
    run_id = str(uuid.uuid4())
    with session_factory.begin() as session:
        ids = sync_platforms(session, configs)
        enabled = enabled_codes(session)
    runs: list[PlatformRun] = []
    for cfg in configs:
        # Ручной запуск по коду площадки идёт и без галочки; плановый — только отмеченные на «Площадках»
        if (only and cfg.code not in only) or (not only and cfg.code not in enabled):
            continue
        started = time.monotonic()
        result = collect(cfg, fetcher, rules.profile.filters.search_queries)
        with session_factory.begin() as session:
            saved = save_deals(session, ids[cfg.code], result.deals)
            error = "; ".join(result.errors) if result.errors else None
            mark_platform(session, ids[cfg.code], None if result.ok else error)
            run = PlatformRun(
                code=cfg.code,
                found=len(result.deals),
                new=saved.new,
                updated=saved.updated,
                skipped=result.skipped,
                closed=result.closed,
                errors=result.errors,
                ms=int((time.monotonic() - started) * 1000),
            )
            session.add(
                AgentStep(
                    run_id=run_id,
                    step="collect",
                    output={
                        "platform": cfg.code,
                        "found": run.found,
                        "new": run.new,
                        "updated": run.updated,
                        "skipped": run.skipped,
                        "closed": run.closed,
                        "errors": run.errors,
                    },
                    ms=run.ms,
                    error=None if result.ok else {"code": "platform_unavailable", "message_ru": error},
                )
            )
        log.info(
            "collect",
            run_id=run_id,
            **{k: v for k, v in run.__dict__.items() if k != "errors"},
            errors=len(run.errors),
        )
        runs.append(run)
    stats = _prefilter_step(session_factory, run_id, rules, vendors)
    started = time.monotonic()
    with session_factory.begin() as session:
        details = load_details(session, fetcher, configs, DETAILS_PER_CYCLE)
        session.add(
            AgentStep(
                run_id=run_id,
                step="details",
                output={"loaded": details.loaded, "errors": details.errors},
                ms=int((time.monotonic() - started) * 1000),
            )
        )
    log.info("details", run_id=run_id, loaded=details.loaded, errors=len(details.errors))
    if details.loaded:
        second = _prefilter_step(session_factory, run_id, rules, vendors)
        stats = {k: stats[k] - (details.loaded if k == "passed" else 0) + second[k] for k in stats}
    return runs, stats


def _prefilter_step(
    session_factory: sessionmaker[Session], run_id: str, rules: Rules, vendors: VendorIndex
) -> dict[str, int]:
    started = time.monotonic()
    with session_factory.begin() as session:
        stats = run_prefilter(session, rules, vendors, datetime.now(UTC))
        session.add(
            AgentStep(
                run_id=run_id,
                step="prefilter",
                output={"rules_version": rules.version, **stats},
                ms=int((time.monotonic() - started) * 1000),
            )
        )
    log.info("prefilter", run_id=run_id, rules_version=rules.version, **stats)
    return stats


def main(argv: list[str]) -> int:
    from app.core.logging import configure_logging
    from app.core.settings import Settings
    from app.db.session import make_engine, make_session_factory

    settings = Settings()  # значения из окружения
    configure_logging(settings.debug)
    factory = make_session_factory(make_engine(settings.database_url.get_secret_value().encoded_string()))
    with factory.begin() as session:
        rules = active_rules(session)
    vendors = load_vendors(VENDORS_FILE)
    with Fetcher(settings.proxy_url) as fetcher:
        platforms = load_platforms(PLATFORMS_FILE)
        runs, stats = run_cycle(factory, fetcher, platforms, rules, vendors, set(argv) or None)
    for r in runs:
        print(
            f"{r.code}: найдено {r.found}, новых {r.new}, обновлено {r.updated}, "
            f"закрытых {r.closed}, не разобрано {r.skipped}, ошибок {len(r.errors)}"
        )
        for e in r.errors:
            print(f"  ! {e}")
    print(
        f"префильтр (правила {rules.version}): прошли {stats['passed']}, "
        f"отсеяно {stats['filtered_out']}, истекли {stats['expired']}"
    )
    return 0 if all(r.found or not r.errors for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
