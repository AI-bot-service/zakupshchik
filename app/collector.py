"""Цикл сбора: все включённые площадки → сделки в БД, по шагу журнала на площадку.

Ручной запуск: python -m app.collector [код_площадки ...]
"""

from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.adapters import collect
from app.adapters.config import PlatformConfig, load_platforms
from app.adapters.fetch import Fetcher
from app.db.models import AgentStep
from app.pipeline.ingest import mark_platform, save_deals, sync_platforms

PLATFORMS_FILE = Path(__file__).resolve().parent.parent / "config" / "platforms.yaml"
log = structlog.get_logger("collector")


@dataclass(frozen=True)
class PlatformRun:
    code: str
    found: int
    new: int
    updated: int
    skipped: int
    errors: list[str]
    ms: int


def run_cycle(
    session_factory: sessionmaker[Session],
    fetcher: Fetcher,
    configs: list[PlatformConfig],
    only: set[str] | None = None,
) -> list[PlatformRun]:
    run_id = str(uuid.uuid4())
    with session_factory.begin() as session:
        ids = sync_platforms(session, configs)
    runs: list[PlatformRun] = []
    for cfg in configs:
        if not cfg.enabled or (only and cfg.code not in only):
            continue
        started = time.monotonic()
        result = collect(cfg, fetcher)
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
                errors=result.errors,
                ms=int((time.monotonic() - started) * 1000),
            )
            session.add(
                AgentStep(
                    run_id=run_id,
                    step="collect",
                    output={"platform": cfg.code, "found": run.found, "new": run.new,
                            "updated": run.updated, "skipped": run.skipped, "errors": run.errors},
                    ms=run.ms,
                    error=None if result.ok else {"code": "platform_unavailable", "message_ru": error},
                )
            )
        log.info("collect", run_id=run_id, **{k: v for k, v in run.__dict__.items() if k != "errors"},
                 errors=len(run.errors))
        runs.append(run)
    return runs


def main(argv: list[str]) -> int:
    from app.core.logging import configure_logging
    from app.core.settings import Settings
    from app.db.session import make_engine, make_session_factory

    settings = Settings()  # значения из окружения
    configure_logging(settings.debug)
    factory = make_session_factory(make_engine(settings.database_url.encoded_string()))
    with Fetcher(settings.proxy_url) as fetcher:
        runs = run_cycle(factory, fetcher, load_platforms(PLATFORMS_FILE), set(argv) or None)
    for r in runs:
        print(f"{r.code}: найдено {r.found}, новых {r.new}, обновлено {r.updated}, "
              f"не разобрано {r.skipped}, ошибок {len(r.errors)}")
        for e in r.errors:
            print(f"  ! {e}")
    return 0 if all(r.found or not r.errors for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
