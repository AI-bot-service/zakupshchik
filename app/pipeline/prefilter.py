"""Префильтр [4] без LLM: ПО ли это, сумма, способ закупки, «только МСП», время. Каждая причина — словами."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.catalog.vendors import VendorIndex
from app.db.models import Deal, DealStatus, Law, Procedure, TriState
from app.scoring.rules import Rules

# Название продукта в кавычках: «Куб CMS», "ТехноКад", „АЖУР“
_QUOTED = re.compile(r"[«\"„“]([^«»\"„“”]{2,60})[»\"“”]")

_ROLE_WORDS = ("заказчик", "поставщик", "исполнител", "покупател", "участник")


def foreign_product(title: str) -> str | None:
    for m in _QUOTED.finditer(title):
        name = m.group(1).strip()
        if not name.lower().startswith(_ROLE_WORDS):
            return name
    return None


PROCEDURE_RU = {
    Procedure.REQUEST_KP: "запрос КП / цен",
    Procedure.SINGLE_SUPPLIER: "у единственного поставщика",
    Procedure.SMALL_PURCHASE: "закупка малого объёма",
    Procedure.QUOTATION_SESSION: "котировочная сессия",
    Procedure.TENDER: "конкурс / тендер",
    Procedure.AUCTION: "аукцион",
    Procedure.REQUEST_QUOTES: "запрос котировок",
    Procedure.REQUEST_PROPOSALS: "запрос предложений",
    Procedure.UNKNOWN: "способ не указан",
}


@dataclass(frozen=True)
class DealFacts:
    title: str
    nmck: Decimal | None
    procedure: Procedure
    law: Law
    end_at: datetime | None
    smb_only: TriState
    # Позиции из карточки; заголовок бывает категорией («Товары информационно-технологические…»)
    items: tuple[str, ...] = ()


@dataclass
class Verdict:
    cut: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    vendors: dict[str, str] = field(default_factory=dict)
    expired: bool = False

    @property
    def passed(self) -> bool:
        return not self.cut and not self.expired


def _money(value: Decimal) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def check(facts: DealFacts, rules: Rules, vendors: VendorIndex, now: datetime) -> Verdict:
    f = rules.profile.filters
    v = Verdict(vendors=vendors.find(" ".join((facts.title, *facts.items))))
    # Есть позиции — «это ПО?» решаем по ним, а не по заголовку
    subject = " ".join(facts.items) if facts.items else facts.title

    if facts.end_at is not None:
        hours_left = (facts.end_at - now).total_seconds() / 3600
        if hours_left <= 0:
            v.expired = True
            v.cut.append("приём заявок закончился")
        elif hours_left < f.min_hours_left:
            v.cut.append(f"до конца приёма {hours_left:.1f} ч — меньше {f.min_hours_left:g} ч, КП не успеть")
    else:
        v.risks.append("срок приёма заявок не указан")

    if not v.vendors:
        software = rules.software.search(subject)
        stop = rules.stop.search(subject)
        quoted = foreign_product(subject) if f.cut_quoted_foreign_product else None
        if not software:
            where = "в позициях" if facts.items else "в названии"
            v.cut.append(f"не ПО: {where} нет вендора из каталога и слов про программы и лицензии")
        elif stop:
            v.cut.append(f"не товар из каталога: «{stop.group(0)}», вендор из каталога не назван")
        elif quoted:
            v.cut.append(f"продукт не из каталога: «{quoted}»")
        else:
            v.risks.append("вендор не назван — сопоставление по описанию")

    if facts.nmck is None:
        v.risks.append("НМЦ не указана")
    elif facts.nmck > f.max_nmck_rub:
        v.cut.append(f"НМЦ {_money(facts.nmck)} больше {_money(f.max_nmck_rub)}")

    if facts.procedure in f.procedures_cut:
        v.cut.append(f"конкурентная процедура: {PROCEDURE_RU[facts.procedure]}")
    elif facts.procedure not in f.procedures_take:
        v.risks.append("способ закупки не указан — проверить извещение")

    smb = rules.smb_only.search(facts.title)
    if facts.smb_only is TriState.YES:
        v.cut.append("только для МСП (флаг площадки)")
    elif smb:
        v.cut.append(f"только для МСП: «{smb.group(0)}» в названии")
    elif facts.law in (Law.FZ44, Law.FZ223):
        v.risks.append("МСП: в карточке не указано — проверить извещение")
    return v


def run_prefilter(session: Session, rules: Rules, vendors: VendorIndex, now: datetime) -> dict[str, int]:
    """Новые сделки и сделки, оценённые прежней версией правил. Решения менеджера не трогаем."""
    deals = session.scalars(
        select(Deal).where(
            Deal.status.in_([DealStatus.NEW, DealStatus.FILTERED_OUT]),
            or_(Deal.rules_version.is_(None), Deal.rules_version != rules.version),
        )
    ).all()
    stats = {"passed": 0, "filtered_out": 0, "expired": 0}
    for deal in deals:
        items = tuple(item.name for item in deal.items)
        verdict = check(
            DealFacts(deal.title, deal.nmck, deal.procedure, deal.law, deal.end_at, deal.smb_only, items),
            rules,
            vendors,
            now,
        )
        deal.rules_version = rules.version
        deal.score_breakdown = {
            **deal.score_breakdown,
            "prefilter": {"cut": verdict.cut, "risks": verdict.risks, "vendors": verdict.vendors},
        }
        if verdict.expired:
            deal.status, deal.filter_reason = DealStatus.EXPIRED, verdict.cut[0]
            stats["expired"] += 1
        elif verdict.cut:
            deal.status, deal.filter_reason = DealStatus.FILTERED_OUT, "; ".join(verdict.cut)
            stats["filtered_out"] += 1
        else:
            deal.status, deal.filter_reason = DealStatus.NEW, None
            stats["passed"] += 1
    return stats
