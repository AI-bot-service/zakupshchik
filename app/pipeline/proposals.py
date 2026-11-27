"""Шаг «КП»: для сделок, подходящих на 100%, и по «Беру в работу» — собрать DOCX и запись proposal.

Без токенов: только сохранённый разбор агента, каталог и rules.yaml.
Позиции не менялись → файл не пересобираем.
Файлы лежат рядом с документами закупки: FILES_DIR/<площадка>/<номер>/КП_<номер>_<дата>.docx (+ .pdf).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import Analysis
from app.db.models import Deal, DealStatus, Platform, Proposal
from app.kp.draft import blockers, build_draft
from app.kp.pdf import to_pdf
from app.kp.render import render_docx
from app.pipeline.analysis import ANALYZED, catalog_items, deal_view
from app.pipeline.attachments import FILES_DIR, deal_dir
from app.scoring.rules import Rules
from app.scoring.score import evaluate


@dataclass
class ProposalRun:
    made: int = 0
    unchanged: int = 0
    not_eligible: int = 0
    pdf: int = 0
    paths: list[str] = field(default_factory=list)


KP_REQUESTED = "kp_requested"  # ключ в score_breakdown: менеджер нажал «Беру в работу»


def request_kp(deal: Deal) -> None:
    deal.score_breakdown = {**deal.score_breakdown, KP_REQUESTED: True}


def kp_requested(deal: Deal) -> bool:
    return bool(deal.score_breakdown.get(KP_REQUESTED))


def make_proposals(
    session: Session, rules: Rules, now: datetime, root: Path = FILES_DIR, only_requested: bool = False
) -> ProposalRun:
    run = ProposalRun()
    codes = dict(session.execute(select(Platform.id, Platform.code)).tuples().all())
    statuses = (*ANALYZED, DealStatus.TAKEN)
    deals = session.scalars(select(Deal).where(Deal.status.in_(statuses))).all()
    for deal in deals:
        requested = kp_requested(deal)
        if only_requested and not requested:
            continue
        if deal.status is DealStatus.TAKEN and not requested:
            continue
        raw = (deal.score_breakdown.get("analysis") or {}).get("result")
        if raw is None:
            continue
        analysis = Analysis.model_validate(raw)
        items = catalog_items(session, analysis)
        result = evaluate(deal_view(deal), analysis, items, rules, now)
        why_not = blockers(deal, analysis, result, items)
        deal.score_breakdown = {**deal.score_breakdown, "kp_blockers": why_not}
        if why_not and not requested:
            run.not_eligible += 1
            continue
        draft = build_draft(deal, analysis, items, rules.profile.kp, now.date(), on_request=requested)
        last = session.scalar(
            select(Proposal).where(Proposal.deal_id == deal.id).order_by(Proposal.version.desc()).limit(1)
        )
        if last is not None and last.items.get("lines") == draft.as_json()["lines"]:
            run.unchanged += 1
            continue
        version = 1 if last is None else last.version + 1
        name = f"КП_{deal.external_id}_{draft.issued:%Y-%m-%d}" + (f"_v{version}" if version > 1 else "")
        path = deal_dir(root, codes[deal.platform_id], deal.external_id) / f"{name}.docx"
        render_docx(draft, path)
        pdf = to_pdf(path)
        run.pdf += pdf is not None
        session.add(
            Proposal(
                deal_id=deal.id,
                version=version,
                items={**draft.as_json(), "pdf": str(pdf) if pdf else None},
                total=draft.total,
                file_path=str(path),
            )
        )
        run.made += 1
        run.paths.append(str(path))
    return run
