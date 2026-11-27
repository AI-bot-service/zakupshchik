"""Шаг «Уведомления»: письмо по горячей сделке и ежедневный Excel. Без токенов — только БД и файлы.

Одно письмо на сделку и адресата (уникальный ключ в notification); сбой — до 3 попыток
за разные циклы, затем failed. Документы закупки докачиваются перед письмом; что не влезает
в MAX_ATTACH_BYTES — списком «открыть на площадке».
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.config import PlatformConfig
from app.adapters.fetch import Fetcher
from app.agent.schemas import Analysis
from app.db.models import (
    Deal,
    DealStatus,
    Finding,
    Notification,
    NotifyChannel,
    NotifyStatus,
    Platform,
    Proposal,
)
from app.notify.kp_link import kp_url
from app.notify.letter_html import deal_html
from app.notify.letters import DealLetter, daily_text, deal_subject, deal_text
from app.notify.postbox import Attachment, Mailer, MailError
from app.pipeline.analysis import catalog_items
from app.pipeline.attachments import AttachmentError, download_file
from app.report.excel import ALL_TIME, Period, build_report
from app.report.stamps import MSK
from app.scoring.rules import Rules

log = structlog.get_logger()
MAX_ATTEMPTS = 3
MAX_ATTACH_BYTES = 5_000_000
MAX_DOCS = 8
_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass
class NotifyRun:
    deal_letters: int = 0
    daily: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _attachment(path: Path, name: str | None = None) -> Attachment:
    return Attachment(
        name or path.name, path.read_bytes(), _MIME.get(path.suffix.lower(), "application/octet-stream")
    )


def _record(
    session: Session, channel: NotifyChannel, to: str, deal_id: int | None, day: date | None
) -> Notification:
    where = [Notification.channel == channel, Notification.recipient == to]
    where.append(Notification.deal_id == deal_id if deal_id is not None else Notification.report_date == day)
    row = session.scalar(select(Notification).where(*where))
    if row is None:
        row = Notification(
            channel=channel,
            recipient=to,
            deal_id=deal_id,
            report_date=day,
            attempts=0,
            status=NotifyStatus.PENDING,
            attachments_sha256=[],
        )
        session.add(row)
    return row


def _send(
    session: Session,
    mailer: Mailer,
    row: Notification,
    subject: str,
    text: str,
    files: list[Attachment],
    html: str | None = None,
) -> bool:
    row.attempts += 1
    try:
        message_id = mailer.send(row.recipient, subject, text, files, html)
    except MailError as exc:
        row.last_error = str(exc)[:500]
        row.status = NotifyStatus.FAILED if row.attempts >= MAX_ATTEMPTS else NotifyStatus.PENDING
        return False
    row.status = NotifyStatus.SENT
    row.last_error = None
    row.message_id, row.subject = message_id[:255], subject
    row.attachments_sha256 = [hashlib.sha256(f.data).hexdigest() for f in files]
    session.flush()
    return True


def _deal_files(
    session: Session, fetcher: Fetcher | None, cfg: PlatformConfig | None, deal: Deal, budget: int
) -> tuple[list[Attachment], list[str], list[str]]:
    files = (deal.raw_masked.get("details") or {}).get("files") or []
    attached: list[Attachment] = []
    names: list[str] = []
    skipped: list[str] = []
    for f in files[:MAX_DOCS] if isinstance(files, list) else []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "файл"))
        if fetcher is None or cfg is None:
            skipped.append(name)
            continue
        try:
            row = download_file(session, fetcher, cfg, deal, str(f.get("id")))
        except AttachmentError:
            skipped.append(name)
            continue
        path = Path(row.path)
        if row.size > budget:
            skipped.append(name)
            continue
        budget -= row.size
        attached.append(_attachment(path, name))
        names.append(name)
    skipped += (
        [str(f.get("name")) for f in files[MAX_DOCS:] if isinstance(f, dict)]
        if isinstance(files, list)
        else []
    )
    return attached, names, skipped


def notify_deals(
    session: Session,
    mailer: Mailer,
    recipients: list[str],
    rules: Rules,
    now: datetime,
    public_url: str,
    fetcher: Fetcher | None = None,
    configs: dict[str, PlatformConfig] | None = None,
    link_secret: str = "",
) -> NotifyRun:
    """link_secret — APP_SECRET_KEY: подпись кнопки «Подготовить КП» (если КП к письму не приложено)."""
    run = NotifyRun()
    min_n = rules.profile.notify.min_conditions
    platforms = {p.id: p for p in session.scalars(select(Platform))}
    deals = session.scalars(
        select(Deal).where(
            Deal.status.in_((DealStatus.HOT, DealStatus.ANALYZED)),
            Deal.conditions_met >= min_n,
            Deal.end_at > now,
        )
    ).all()
    for deal in deals:
        todo = []
        for to in recipients:
            row = _record(session, NotifyChannel.EMAIL_DEAL, to, deal.id, None)
            if row.status is NotifyStatus.PENDING:
                todo.append(row)
        if not todo:
            continue
        raw = (deal.score_breakdown.get("analysis") or {}).get("result")
        if raw is None:
            continue
        analysis = Analysis.model_validate(raw)
        platform = platforms[deal.platform_id]
        kp_files: list[Attachment] = []
        proposal = session.scalar(
            select(Proposal).where(Proposal.deal_id == deal.id).order_by(Proposal.version.desc()).limit(1)
        )
        if proposal is not None and proposal.file_path and Path(proposal.file_path).exists():
            kp_files.append(_attachment(Path(proposal.file_path)))
            pdf = proposal.items.get("pdf")
            if pdf and Path(pdf).exists():
                kp_files.append(_attachment(Path(pdf)))
        budget = MAX_ATTACH_BYTES - sum(len(f.data) for f in kp_files)
        cfg = (configs or {}).get(platform.code)
        docs, docs_names, docs_skipped = _deal_files(session, fetcher, cfg, deal, budget)
        findings = [
            f.text
            for f in session.scalars(select(Finding).where(Finding.deal_id == deal.id).order_by(Finding.id))
        ]
        letter = DealLetter(
            deal=deal,
            platform=platform.name,
            analysis=analysis,
            items=catalog_items(session, analysis),
            findings=findings,
            kp_files=[f.name for f in kp_files],
            docs_attached=docs_names,
            docs_not_attached=docs_skipped,
            card_url=f"{public_url}/deals/{deal.id}",
        )
        subject = deal_subject(deal, analysis, now)
        excel, _ = build_report(session, rules, now, Period(None, None, False, f"сделка №{deal.id}", deal.id))
        sheet = Attachment(f"zakupshchik-{deal.external_id}.xlsx", excel, _MIME[".xlsx"])
        session.flush()  # id записи письма — в подписи кнопки «Подготовить КП»
        for row in todo:
            mine = letter
            if link_secret and not kp_files:
                mine = replace(letter, kp_url=kp_url(public_url, link_secret, row.id))
            text, html = deal_text(mine, now), deal_html(mine, now)
            if _send(session, mailer, row, subject, text, [sheet, *kp_files, *docs], html):
                run.deal_letters += 1
            else:
                run.failed += 1
                run.errors.append(f"сделка {deal.id}: {row.last_error}")
    return run


def daily_report(
    session: Session, mailer: Mailer, recipients: list[str], rules: Rules, now: datetime, public_url: str
) -> NotifyRun:
    """Раз в день после notify.daily_report_at (МСК): вчера + всё с открытой подачей, и вся история."""
    run = NotifyRun()
    local = now.astimezone(MSK)
    if local.time() < rules.profile.notify.daily_report_at:
        return run
    day = local.date()
    todo = [
        row
        for to in recipients
        if (row := _record(session, NotifyChannel.EMAIL_DAILY, to, None, day)).status is NotifyStatus.PENDING
    ]
    if not todo:
        return run
    since = datetime.combine(day - timedelta(days=1), datetime.min.time(), tzinfo=MSK)
    data, stats = build_report(session, rules, now, Period(since, None, True, "вчера и сегодня"))
    # Полная история во вложении: если стенд недоступен, у менеджера в почте копия не старше суток
    history, _ = build_report(session, rules, now, ALL_TIME)
    top = session.scalars(
        select(Deal)
        .where(Deal.status.in_((DealStatus.HOT, DealStatus.ANALYZED)), Deal.end_at > now)
        .order_by(Deal.conditions_met.desc().nulls_last(), Deal.end_at)
        .limit(5)
    ).all()
    name = f"zakupshchik-{day:%Y-%m-%d}.xlsx"
    files = [
        Attachment(name, data, _MIME[".xlsx"]),
        Attachment(f"zakupshchik-history-{day:%Y-%m-%d}.xlsx", history, _MIME[".xlsx"]),
    ]
    subject = f"Закупщик: сделки на {day:%d.%m.%Y} — {stats.deals} в отчёте"
    text = daily_text(list(top), stats.deals, stats.excluded, now, f"{public_url}/deals")
    for row in todo:
        if _send(session, mailer, row, subject, text, files):
            run.daily += 1
        else:
            run.failed += 1
            run.errors.append(f"отчёт {day}: {row.last_error}")
    return run
