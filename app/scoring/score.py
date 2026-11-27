"""Проверка ограничений (S6) и оценка «N из 6» (S7) без LLM: факты разбора + правила → итог с разложением.

Пересчёт при смене rules.yaml идёт по сохранённому разбору, без токенов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agent.schemas import Analysis, ConstraintKind
from app.db.models import (
    CatalogItem,
    ContractFormat,
    FindingKind,
    MatchKind,
    PaymentTerms,
    Procedure,
    TriState,
)
from app.scoring.rules import Rules

_TZ_FILE = re.compile(r"(?i)(?<!\w)(тз|техническ\w*\s+задани\w*|описани\w*\s+объект\w*|спецификаци\w*)")


class Condition(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    key: str
    title: str
    met: bool
    reason: str


class FindingOut(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: FindingKind
    text: str
    quote: str | None = None


@dataclass(frozen=True)
class DealView:
    """Поля сделки, нужные оценке: так оценку можно проверить на фикстурах без БД."""

    title: str
    nmck: Decimal | None
    procedure: Procedure
    end_at: datetime | None
    smb_only: TriState
    file_names: tuple[str, ...]
    guarantee_flag: bool


CONDITIONS_TOTAL = 6

# В магазине цена часто за пакет: «на 30 ПК» — за 30 мест, «по количеству узлов» — за диапазон узлов,
# и диапазон в выгрузке ненадёжен. Умножать такую цену на количество из закупки нельзя.
_PACK = re.compile(
    r"(?:на|,)\s*(\d+)\s*(?:пк|рабоч\w*|устройств\w*|пользовател\w*|узл\w*|компьютер\w*|станц\w*)", re.I
)
_BAND = re.compile(
    r"по количеству|количество узлов|к-во узлов|\d+\s*[-–]\s*\d+\s*(?:узл|пк|пользовател)", re.I
)


def pack_size(name: str) -> Decimal | None:
    """Цена товара — за пакет из N мест («на 30 ПК»)."""
    m = _PACK.search(name)
    return Decimal(m.group(1)) if m and int(m.group(1)) > 0 else None


def priced_by_band(name: str) -> bool:
    """Цена — за диапазон узлов («по количеству узлов», «50-99 узлов»): на число мест не пересчитать."""
    return _BAND.search(name) is not None


def line_cost(name: str, price: Decimal, qty: float | None) -> Decimal | None:
    """Стоимость позиции по цене каталога. None — цена за диапазон, честно посчитать нельзя."""
    need = Decimal(str(qty or 1))
    if (size := pack_size(name)) is not None:
        return price * (need / size).to_integral_value(rounding=ROUND_CEILING)
    if priced_by_band(name):
        return None
    return price * need


def catalog_met(score_breakdown: dict[str, Any] | None) -> bool:
    """Все позиции закупки нашлись в каталоге. Нет — сделку не отсеиваем, а помечаем для решения менеджера."""
    conditions = ((score_breakdown or {}).get("score") or {}).get("conditions") or []
    return any(c.get("key") == "catalog" and c.get("met") for c in conditions)


@dataclass
class ScoreResult:
    conditions: list[Condition]
    findings: list[FindingOut]
    components: dict[str, float]
    score: float
    catalog_total: Decimal | None
    has_tz: bool
    positions_without_price: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return sum(c.met for c in self.conditions)

    @property
    def hot(self) -> bool:
        return self.n == len(self.conditions) and not any(
            f.kind is FindingKind.CONFLICT for f in self.findings
        )


def _rub(value: Decimal) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def evaluate(
    deal: DealView,
    analysis: Analysis,
    items: dict[int, CatalogItem],
    rules: Rules,
    now: datetime,
) -> ScoreResult:
    cond_rules = rules.profile.conditions
    findings: list[FindingOut] = []
    kinds = {c.kind for c in analysis.constraints}
    quote_of = {c.kind: c.quote for c in analysis.constraints}

    # ── S6: ограничения ────────────────────────────────────────────────────
    if not analysis.is_license_supply:
        findings.append(
            FindingOut(
                kind=FindingKind.CONFLICT,
                text=f"не поставка лицензий: {analysis.not_license_reason or 'услуги'}",
            )
        )
    matched = [p for p in analysis.positions if p.match is not MatchKind.NONE and p.catalog_item_id in items]
    missing_pos = [p for p in analysis.positions if p not in matched]
    if analysis.positions and not matched:
        findings.append(FindingOut(kind=FindingKind.CONFLICT, text="в каталоге нет ни одной позиции закупки"))
    elif missing_pos:
        names = ", ".join(p.name[:60] for p in missing_pos[:3])
        findings.append(FindingOut(kind=FindingKind.RISK, text=f"часть позиций не из каталога: {names}"))
    if ConstraintKind.NO_EQUIVALENT in kinds and any(p.match is MatchKind.ANALOG for p in matched):
        findings.append(
            FindingOut(
                kind=FindingKind.CONFLICT,
                text="эквивалент не допускается, а в каталоге только аналог",
                quote=quote_of[ConstraintKind.NO_EQUIVALENT],
            )
        )
    if ConstraintKind.DOMESTIC_ONLY in kinds:
        foreign = [
            items[p.catalog_item_id].name
            for p in matched
            if p.catalog_item_id is not None and not items[p.catalog_item_id].rf_registry_no
        ]
        if foreign:
            findings.append(
                FindingOut(
                    kind=FindingKind.RISK,
                    text=f"нужно ПО из реестра Минцифры, у «{foreign[0][:60]}» в каталоге нет номера реестра",
                    quote=quote_of[ConstraintKind.DOMESTIC_ONLY],
                )
            )
    if ConstraintKind.SMB_ONLY in kinds:
        findings.append(
            FindingOut(
                kind=FindingKind.CONFLICT,
                text="участвовать могут только МСП",
                quote=quote_of[ConstraintKind.SMB_ONLY],
            )
        )
    if ConstraintKind.PARTICIPANT_LICENSE in kinds:
        findings.append(
            FindingOut(
                kind=FindingKind.RISK,
                text="требование к участнику (лицензия или статус партнёра)",
                quote=quote_of[ConstraintKind.PARTICIPANT_LICENSE],
            )
        )
    if ConstraintKind.GUARANTEE in kinds or deal.guarantee_flag:
        findings.append(
            FindingOut(
                kind=FindingKind.RISK,
                text="нужно обеспечение заявки или договора",
                quote=quote_of.get(ConstraintKind.GUARANTEE),
            )
        )
    for p in analysis.positions:
        if p.qty is None:
            findings.append(
                FindingOut(kind=FindingKind.REQUIREMENT, text=f"не указано количество: {p.name[:60]}")
            )
    for text in analysis.missing[:5]:
        findings.append(FindingOut(kind=FindingKind.REQUIREMENT, text=text[:200]))

    no_price: list[str] = []
    band_price: list[str] = []
    total = Decimal(0)
    for p in matched:
        item = items[p.catalog_item_id or 0]  # matched отобраны с id из items
        if item.price is None:
            no_price.append(item.name)
        elif (cost := line_cost(item.name, item.price, p.qty)) is None:
            band_price.append(item.name)
        else:
            total += cost
    if no_price:
        findings.append(
            FindingOut(
                kind=FindingKind.RISK, text=f"нет цены в каталоге: {no_price[0][:60]} — запросить у вендора"
            )
        )
    if band_price:
        findings.append(
            FindingOut(
                kind=FindingKind.RISK,
                text=f"цена в каталоге за диапазон узлов: {band_price[0][:60]} — пересчитать на нужное число",
            )
        )
    catalog_total = total if matched and not no_price and not band_price else None
    if catalog_total is not None and deal.nmck is not None and catalog_total > deal.nmck:
        findings.append(
            FindingOut(
                kind=FindingKind.RISK,
                text=f"по ценам каталога {_rub(catalog_total)} — больше НМЦ {_rub(deal.nmck)}",
            )
        )

    # ── S7: шесть условий ───────────────────────────────────────────────────
    conflicts = [f for f in findings if f.kind is FindingKind.CONFLICT]
    if matched and not missing_pos and analysis.is_license_supply:
        names = ", ".join(sorted({items[p.catalog_item_id].vendor for p in matched if p.catalog_item_id}))
        c1 = Condition(
            key="catalog",
            title="ПО из каталога",
            met=not conflicts,
            reason=f"все позиции есть в каталоге ({names})" if not conflicts else conflicts[0].text,
        )
    else:
        why = (
            conflicts[0].text
            if conflicts
            else (
                "позиции не разобраны"
                if not analysis.positions
                else f"не найдено: {missing_pos[0].name[:60]}"
            )
        )
        c1 = Condition(key="catalog", title="ПО из каталога", met=False, reason=why)

    limit = rules.profile.filters.max_nmck_rub
    amount_title = f"НМЦ до {_rub(limit)}"
    if deal.nmck is None:
        c2 = Condition(key="amount", title=amount_title, met=False, reason="НМЦ не указана — проверить")
    else:
        c2 = Condition(
            key="amount",
            title=amount_title,
            met=deal.nmck <= limit,
            reason=f"НМЦ {_rub(deal.nmck)}" + ("" if deal.nmck <= limit else f" > {_rub(limit)}"),
        )

    simple_blockers = []
    if deal.procedure not in rules.profile.filters.procedures_take:
        simple_blockers.append("способ закупки не из быстрых")
    if ConstraintKind.SMB_ONLY in kinds or deal.smb_only is TriState.YES:
        simple_blockers.append("только МСП")
    if ConstraintKind.GUARANTEE in kinds or deal.guarantee_flag:
        simple_blockers.append("обеспечение")
    if ConstraintKind.PARTICIPANT_LICENSE in kinds:
        simple_blockers.append("требования к участнику")
    c3 = Condition(
        key="simple",
        title="Упрощённая процедура",
        met=not simple_blockers,
        reason="; ".join(simple_blockers) or "без конкурса, обеспечения и требований к участнику",
    )

    days = analysis.payment_days
    if analysis.payment_terms is PaymentTerms.PREPAYMENT:
        c4 = Condition(key="payment", title="Оплата", met=True, reason="предоплата")
    elif analysis.payment_terms in (PaymentTerms.POSTPAYMENT, PaymentTerms.ON_DELIVERY) and days is not None:
        ok = days <= cond_rules.max_postpay_days
        c4 = Condition(
            key="payment",
            title="Оплата",
            met=ok,
            reason=f"постоплата {days} дн." + ("" if ok else f" — больше {cond_rules.max_postpay_days}"),
        )
    elif analysis.payment_terms is PaymentTerms.ON_DELIVERY:
        c4 = Condition(key="payment", title="Оплата", met=True, reason="оплата при поставке")
    else:
        c4 = Condition(
            key="payment", title="Оплата", met=False, reason="порядок оплаты не указан — проверить"
        )

    if deal.end_at is None:
        c5 = Condition(key="time", title="Успеваем подать", met=False, reason="срок подачи не указан")
    else:
        hours = (deal.end_at - now).total_seconds() / 3600
        ok = hours >= cond_rules.min_hours_for_kp
        c5 = Condition(
            key="time",
            title="Успеваем подать",
            met=ok,
            reason=f"до конца подачи {hours:.0f} ч"
            + ("" if ok else f" — меньше {cond_rules.min_hours_for_kp:g}"),
        )
    if catalog_total is not None and deal.nmck is not None:
        fits = catalog_total <= deal.nmck
        c6 = Condition(
            key="fits",
            title="Укладываемся в НМЦ",
            met=fits,
            reason=(
                f"по ценам каталога {_rub(catalog_total)} при НМЦ {_rub(deal.nmck)}"
                if fits
                else f"по ценам каталога {_rub(catalog_total)} — больше НМЦ {_rub(deal.nmck)}"
            ),
        )
    else:
        why_unknown = (
            "НМЦ не указана — сравнить вручную"
            if deal.nmck is None
            else "в каталоге нет цены — сравнить с НМЦ после ответа вендора"
            if no_price
            else "цена в каталоге за диапазон узлов — пересчитать и сравнить с НМЦ"
            if band_price
            else "нечего сравнивать: позиций нет в каталоге"
        )
        c6 = Condition(key="fits", title="Укладываемся в НМЦ", met=False, reason=why_unknown)
    conditions = [c1, c2, c3, c4, c5, c6]

    # ── приоритет внутри группы ────────────────────────────────────────────
    w = rules.profile.weights
    has_tz = any(_TZ_FILE.search(n) for n in deal.file_names)
    coverage = len(matched) / len(analysis.positions) if analysis.positions else 0.0
    margin = 0.5 if catalog_total is None or deal.nmck is None else float(catalog_total <= deal.nmck)
    amount = float(min(deal.nmck / limit, Decimal(1))) if deal.nmck is not None else 0.0
    payment = 1.0 if analysis.payment_terms is PaymentTerms.PREPAYMENT else (0.7 if c4.met else 0.0)
    simplicity = 1.0 - 0.5 * has_tz - 0.5 * (analysis.contract_format is ContractFormat.CUSTOMER_TEMPLATE)
    components = {
        "coverage": round(coverage, 3),
        "margin": margin,
        "amount": round(amount, 3),
        "payment": payment,
        "simplicity": simplicity,
    }
    score = (
        w.coverage * coverage
        + w.margin * margin
        + w.amount * amount
        + w.payment * payment
        + w.simplicity * simplicity
    )
    return ScoreResult(conditions, findings, components, round(score, 4), catalog_total, has_tz, no_price)
