"""Фоновый процесс стенда: цикл сбора по расписанию. Один цикл за раз; сбой цикла не роняет процесс.

Запуск: python -m app.worker  (интервал — COLLECT_INTERVAL_MIN, по умолчанию 5 минут)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.config import load_platforms
from app.adapters.fetch import Fetcher
from app.catalog.importer import DEFAULT_PATH as CATALOG_FILE
from app.catalog.importer import import_cards
from app.catalog.vendors import load_vendors
from app.collector import PLATFORMS_FILE, VENDORS_FILE, run_cycle
from app.core.logging import configure_logging
from app.core.settings import Settings
from app.db.session import make_engine, make_session_factory
from app.scoring.rules import RulesError, load_rules

log = structlog.get_logger("worker")


def cycle(factory: sessionmaker[Session], proxy_url: str | None) -> None:
    try:
        # Правила и площадки перечитываются каждый цикл: правка файла действует без перезапуска
        rules = load_rules()
        vendors = load_vendors(VENDORS_FILE)
        platforms = load_platforms(PLATFORMS_FILE)
    except (RulesError, ValueError, OSError) as exc:
        log.error("cycle_skipped", reason=str(exc)[:500])
        return
    try:
        with Fetcher(proxy_url) as fetcher:
            runs, stats = run_cycle(factory, fetcher, platforms, rules, vendors)
        log.info("cycle_done", platforms=len(runs), found=sum(r.found for r in runs), **stats)
    except Exception as exc:  # сбой одного цикла (БД, сеть) — следующий по расписанию
        log.exception("cycle_failed", error=type(exc).__name__)


def import_catalog(factory: sessionmaker[Session]) -> None:
    if not CATALOG_FILE.exists():
        log.warning("catalog_missing", path=str(CATALOG_FILE))
        return
    with factory.begin() as session:
        count, errors = import_cards(session, CATALOG_FILE)
    log.info("catalog_imported", items=count, errors=len(errors))


def main() -> None:
    settings = Settings()  # значения из окружения
    configure_logging(settings.debug)
    factory = make_session_factory(make_engine(settings.database_url.encoded_string()))
    interval = int(os.environ.get("COLLECT_INTERVAL_MIN", "5"))
    import_catalog(factory)
    scheduler = BlockingScheduler(timezone=UTC)
    scheduler.add_job(
        cycle,
        "interval",
        minutes=interval,
        args=[factory, settings.proxy_url],
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        id="collect",
    )
    # Каталог: выгрузка store дописывается — подхватываем раз в 6 часов
    scheduler.add_job(import_catalog, "interval", hours=6, args=[factory], id="catalog", max_instances=1)
    log.info("worker_started", interval_min=interval)
    scheduler.start()


if __name__ == "__main__":
    main()
