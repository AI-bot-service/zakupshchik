"""Фоновый процесс стенда: цикл сбора по расписанию. Один цикл за раз; сбой цикла не роняет процесс.

Запуск: python -m app.worker  (интервал — COLLECT_INTERVAL_MIN, по умолчанию 5 минут)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.config import PlatformConfig, load_platforms
from app.adapters.fetch import Fetcher
from app.agent.llm import GatewayLLM
from app.catalog.importer import DEFAULT_PATH as CATALOG_FILE
from app.catalog.importer import import_cards
from app.catalog.vendors import load_vendors
from app.collector import PLATFORMS_FILE, VENDORS_FILE, run_cycle
from app.core.logging import configure_logging
from app.core.settings import Settings
from app.db.session import make_engine, make_session_factory
from app.notify.postbox import MailError, PostboxMailer
from app.pipeline.analysis import Models, refresh_explanations, rescore, run_analysis
from app.pipeline.notify import NotifyRun, daily_report, notify_deals
from app.pipeline.proposals import make_proposals
from app.pipeline.runs import finish, take_pending
from app.scoring.rules import Rules, RulesError
from app.scoring.store import active_rules

log = structlog.get_logger("worker")
# Сделок на разбор за запуск: ~1 мин на сделку, запуск раз в интервал сбора
ANALYZE_PER_RUN = int(os.environ.get("ANALYZE_PER_RUN", "4"))
# Сбор и разбор не идут параллельно сами с собой: ручной запуск со стенда ждёт планового и наоборот
COLLECT_LOCK = threading.Lock()
ANALYZE_LOCK = threading.Lock()


def cycle(factory: sessionmaker[Session], proxy_url: str | None) -> dict[str, Any]:
    if not COLLECT_LOCK.acquire(blocking=False):
        log.info("cycle_busy")
        return {}
    try:
        return _cycle(factory, proxy_url)
    finally:
        COLLECT_LOCK.release()


def _cycle(factory: sessionmaker[Session], proxy_url: str | None) -> dict[str, Any]:
    try:
        # Правила (БД, «Админка») и площадки читаются каждый цикл: правка действует без перезапуска
        with factory.begin() as session:
            rules = active_rules(session)
        vendors = load_vendors(VENDORS_FILE)
        platforms = load_platforms(PLATFORMS_FILE)
    except (RulesError, ValueError, OSError, SQLAlchemyError) as exc:
        log.error("cycle_skipped", reason=str(exc)[:500])
        return {"error": str(exc)[:500]}
    try:
        with Fetcher(proxy_url) as fetcher:
            runs, stats = run_cycle(factory, fetcher, platforms, rules, vendors)
        found = sum(r.found for r in runs)
        log.info("cycle_done", platforms=len(runs), found=found, **stats)
        return {"found": found, "new": sum(r.new for r in runs), **stats}
    except Exception as exc:  # сбой одного цикла (БД, сеть) — следующий по расписанию
        log.exception("cycle_failed", error=type(exc).__name__)
        return {"error": type(exc).__name__}


def notify(
    factory: sessionmaker[Session],
    settings: Settings,
    rules: Rules,
    platforms: list[PlatformConfig],
    now: datetime,
) -> NotifyRun:
    """Письма по горячим сделкам и ежедневный Excel. Нет Postbox или адресатов — пропускаем."""
    if not settings.recipients:
        return NotifyRun()
    try:
        mailer = PostboxMailer(
            settings.postbox_access_key_id,
            settings.postbox_secret_key.get_secret_value(),
            settings.postbox_from,
        )
    except MailError as exc:
        log.warning("notify_skipped", reason=str(exc))
        return NotifyRun()
    by_code = {c.code: c for c in platforms}
    with Fetcher(settings.proxy_url) as fetcher, factory.begin() as session:
        run = notify_deals(
            session, mailer, settings.recipients, rules, now, settings.public_url, fetcher, by_code
        )
    with factory.begin() as session:
        daily = daily_report(session, mailer, settings.recipients, rules, now, settings.public_url)
    run.daily, run.failed = daily.daily, run.failed + daily.failed
    for err in run.errors + daily.errors:
        log.warning("notify_failed", error=err)
    return run


def analyze(factory: sessionmaker[Session], settings: Settings) -> dict[str, Any]:
    if not ANALYZE_LOCK.acquire(blocking=False):
        log.info("analyze_busy")
        return {}
    try:
        return _analyze(factory, settings)
    finally:
        ANALYZE_LOCK.release()


def _analyze(factory: sessionmaker[Session], settings: Settings) -> dict[str, Any]:
    try:
        with factory.begin() as session:
            rules = active_rules(session)
        vendors = load_vendors(VENDORS_FILE)
        platforms = load_platforms(PLATFORMS_FILE)
        llm = GatewayLLM(settings.llm_base_url, settings.llm_api_key.get_secret_value())
    except (RulesError, ValueError, OSError, SQLAlchemyError) as exc:
        log.error("analyze_skipped", reason=str(exc)[:500])
        return {"error": str(exc)[:500]}
    try:
        now = datetime.now(UTC)
        with factory.begin() as session:
            stats = rescore(session, rules, now)
        models = Models(settings.llm_model_main, settings.llm_model_cheap)
        refreshed, refresh_cost = refresh_explanations(factory, llm, models.cheap, rules, now)
        if refreshed:
            log.info("explanations_refreshed", deals=refreshed, cost_rub=str(refresh_cost))
        with Fetcher(settings.proxy_url) as fetcher:
            run = run_analysis(factory, fetcher, platforms, vendors, rules, llm, models, now, ANALYZE_PER_RUN)
        with factory.begin() as session:
            kp = make_proposals(session, rules, now)
        sent = notify(factory, settings, rules, platforms, now)
        log.info(
            "analyze_done",
            analyzed=run.analyzed,
            hot=run.hot,
            needs_human=run.needs_human,
            same_input=run.skipped_same_input,
            cost_rub=str(run.cost_rub),
            errors=len(run.errors),
            kp_made=kp.made,
            kp_pdf=kp.pdf,
            letters=sent.deal_letters,
            daily=sent.daily,
            mail_failed=sent.failed,
            kp_not_eligible=kp.not_eligible,
            **stats,
        )
        return {
            "analyzed": run.analyzed,
            "hot": run.hot,
            "needs_human": run.needs_human,
            "cost_rub": str(run.cost_rub.quantize(Decimal("0.01"))),
            "kp_made": kp.made,
            "letters": sent.deal_letters,
        }
    except Exception as exc:  # сбой разбора — следующий по расписанию
        log.exception("analyze_failed", error=type(exc).__name__)
        return {"error": type(exc).__name__}


def manual(factory: sessionmaker[Session], settings: Settings) -> None:
    """Заявка «Запустить цикл» со стенда: сбор, затем разбор. Идёт плановый — ждём его, не пропускаем."""
    with factory.begin() as session:
        row = take_pending(session)
        if row is None:
            return
        row_id, by = row.id, row.output.get("by")
    log.info("manual_run_started", row=row_id, by=by)
    started = time.monotonic()
    summary: dict[str, Any] = {}
    try:
        with COLLECT_LOCK:
            summary["collect"] = _cycle(factory, settings.proxy_url)
        with ANALYZE_LOCK:
            summary["analyze"] = _analyze(factory, settings)
    except Exception as exc:
        log.exception("manual_run_failed", error=type(exc).__name__)
        summary["error"] = type(exc).__name__
    failed = "error" in summary or any("error" in part for part in summary.values() if isinstance(part, dict))
    with factory.begin() as session:
        finish(session, row_id, summary, int((time.monotonic() - started) * 1000), failed)
    log.info("manual_run_done", row=row_id, failed=failed)


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
    # Опрос заявок раз в 10 с иначе засоряет журнал строками «Job executed successfully»; итоги пишем сами
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
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
    # Разбор агентом отдельно от сбора: долгие вызовы LLM не задерживают обход площадок
    scheduler.add_job(
        analyze,
        "interval",
        minutes=interval,
        args=[factory, settings],
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        id="analyze",
    )
    # Заявки «Запустить цикл» со стенда
    scheduler.add_job(
        manual, "interval", seconds=10, args=[factory, settings], id="manual", max_instances=1, coalesce=True
    )
    # Каталог: выгрузка store дописывается — подхватываем раз в 6 часов
    scheduler.add_job(import_catalog, "interval", hours=6, args=[factory], id="catalog", max_instances=1)
    log.info("worker_started", interval_min=interval)
    scheduler.start()


if __name__ == "__main__":
    main()
