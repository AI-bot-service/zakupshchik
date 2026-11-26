"""Что попадёт в КП (§7.1): только для полностью подходящей сделки.

Наименования, артикулы и цены — из каталога, количество — из закупки. Нет цены в каталоге → строка
с пустой ценой (заливка в файле), итог «без учёта позиций с пустой ценой». ПО из реестра Минцифры —
без НДС, остальное — НДС по ставке из rules.yaml (цена каталога включает НДС).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.agent.schemas import Analysis
from app.db.models import CatalogItem, Deal, MatchKind, PaymentTerms, TriState
from app.scoring.rules import Kp
from app.scoring.score import ScoreResult

_CENT = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class KpLine:
    sku: str
    name: str
    qty: Decimal
    unit_price: Decimal | None
    vat_free: bool
    catalog_item_id: int

    @property
    def amount(self) -> Decimal | None:
        return None if self.unit_price is None else _round(self.unit_price * self.qty)

    def vat(self, rate: Decimal) -> Decimal | None:
        if self.amount is None or self.vat_free:
            return None
        return _round(self.amount * rate / (100 + rate))


@dataclass(frozen=True)
class KpDraft:
    customer: str
    issued: date
    lines: list[KpLine]
    vat_rate: Decimal
    payment: str
    delivery: str
    valid_until: date
    signer_name: str
    signer_title: str

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines if ln.amount is not None), Decimal(0))

    @property
    def vat_total(self) -> Decimal | None:
        """None — НДС нет ни в одной строке (всё из реестра)."""
        vats = [v for v in (ln.vat(self.vat_rate) for ln in self.lines) if v is not None]
        return sum(vats, Decimal(0)) if vats else None

    @property
    def blank_prices(self) -> int:
        return sum(ln.unit_price is None for ln in self.lines)

    def as_json(self) -> dict[str, Any]:
        return {
            "lines": [
                {
                    "catalog_item_id": ln.catalog_item_id,
                    "sku": ln.sku,
                    "name": ln.name,
                    "qty": str(ln.qty),
                    "unit_price": None if ln.unit_price is None else str(ln.unit_price),
                    "vat_free": ln.vat_free,
                }
                for ln in self.lines
            ],
            "payment": self.payment,
            "delivery": self.delivery,
            "vat_rate": str(self.vat_rate),
        }


def blockers(deal: Deal, analysis: Analysis, result: ScoreResult, items: dict[int, CatalogItem]) -> list[str]:
    """Почему КП не делаем; пусто — сделка подходит на 100%."""
    out: list[str] = []
    if not analysis.is_license_supply:
        out.append("не поставка лицензий")
    if not analysis.positions:
        out.append("нет позиций")
    for p in analysis.positions:
        if p.match is not MatchKind.EXACT or p.catalog_item_id not in items:
            out.append(f"«{p.name[:60]}»: нет точного совпадения в каталоге")
        elif p.qty is None:
            out.append(f"«{p.name[:60]}»: не указано количество")
    if deal.smb_only is TriState.YES:
        out.append("только МСП")
    out.extend(f"конфликт: {f.text}" for f in result.findings if f.kind.value == "conflict")
    return out


def _payment(analysis: Analysis, kp: Kp) -> str:
    days = f" {analysis.payment_days} дней" if analysis.payment_days else ""
    match analysis.payment_terms:
        case PaymentTerms.PREPAYMENT:
            return "Предоплата"
        case PaymentTerms.POSTPAYMENT:
            return f"Постоплата{days}"
        case PaymentTerms.ON_DELIVERY:
            return f"Оплата по факту поставки{days}"
        case _:
            return kp.default_payment


def _delivery(analysis: Analysis, used: list[CatalogItem], kp: Kp) -> str:
    store = sorted({i.delivery_days.strip() for i in used if i.delivery_days and i.delivery_days.strip()})
    text = "; ".join(store) if store else kp.default_delivery
    if analysis.delivery_days is not None:
        text += f", не позднее {analysis.delivery_days} дней"
    return text


def build_draft(
    deal: Deal, analysis: Analysis, items: dict[int, CatalogItem], kp: Kp, today: date
) -> KpDraft:
    lines: list[KpLine] = []
    used: list[CatalogItem] = []
    for p in analysis.positions:
        if p.catalog_item_id is None or p.qty is None:
            raise ValueError("позиция без товара или количества — сначала blockers()")
        item = items[p.catalog_item_id]
        used.append(item)
        lines.append(
            KpLine(
                sku=item.sku,
                name=item.name,
                qty=Decimal(str(p.qty)),
                unit_price=item.price,
                vat_free=item.rf_registry_no is not None,
                catalog_item_id=item.id,
            )
        )
    valid = max(kp.valid_days, deal.kp_min_valid_days or 0)
    return KpDraft(
        customer=deal.customer_name or "Заказчику",
        issued=today,
        lines=lines,
        vat_rate=kp.vat_rate,
        payment=_payment(analysis, kp),
        delivery=_delivery(analysis, used, kp),
        valid_until=today + timedelta(days=valid),
        signer_name=kp.signer_name,
        signer_title=kp.signer_title,
    )
