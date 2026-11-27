"""Excel-отчёт менеджеру (§7, §7.3): «Сделки», «Исключённые», «Как считали».

Собирается из БД за период (дата, когда робот нашёл сделку), обратно не читается. История не удаляется:
сделки с истёкшим сроком остаются со статусом «срок истёк». Сортировка: «N из 5» → срок подачи → приоритет.
Нет цены по каталогу — пустая ячейка с заливкой (как в КП). Время — московское.
Чтобы файл открывался ровно и в Excel, и в LibreOffice: числа и даты — значения с форматом, а не текст;
высота строк с переносом считается здесь (Excel сам её не подбирает).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import ColumnElement, and_, or_, select, true
from sqlalchemy.orm import Session

from app.core.provenance import BUILD_MARK
from app.db.models import (
    ContractFormat,
    Deal,
    DealStatus,
    Finding,
    PaymentTerms,
    Platform,
    Proposal,
    TriState,
)
from app.pipeline.prefilter import PROCEDURE_RU
from app.scoring.rules import Rules

MSK = timezone(timedelta(hours=3), "MSK")
BLANK_FILL = PatternFill("solid", fgColor="FFF2CC")
HOT_FILL = PatternFill("solid", fgColor="E2EFDA")
HEAD_FONT = Font(bold=True)
HEAD_FILL = PatternFill("solid", fgColor="E6E5FB")
MONEY = "#,##0.00"
DATETIME = "DD.MM.YYYY HH:MM"
LINE_PT = 15
MAX_ROW_PT = 300
WRAP = Alignment(wrap_text=True, vertical="top")
SHOWN = (DealStatus.HOT, DealStatus.ANALYZED, DealStatus.TAKEN, DealStatus.SUBMITTED, DealStatus.SKIPPED)
ANALYZED_KEY = "analysis"
STATUS_RU = {
    DealStatus.NEW: "в очереди на разбор",
    DealStatus.HOT: "горячая",
    DealStatus.ANALYZED: "разобрана",
    DealStatus.TAKEN: "беру",
    DealStatus.SUBMITTED: "подано",
    DealStatus.SKIPPED: "не беру",
    DealStatus.EXPIRED: "срок истёк",
    DealStatus.FILTERED_OUT: "отсеяна",
}
PAYMENT_RU = {
    PaymentTerms.PREPAYMENT: "предоплата",
    PaymentTerms.POSTPAYMENT: "постоплата",
    PaymentTerms.ON_DELIVERY: "по факту поставки",
    PaymentTerms.UNKNOWN: "не указано",
}
CONTRACT_RU = {
    ContractFormat.NONE: "без договора (по счёту)",
    ContractFormat.SUPPLIER_TEMPLATE: "шаблон поставщика",
    ContractFormat.CUSTOMER_TEMPLATE: "проект заказчика",
    ContractFormat.UNKNOWN: "не указано",
}
TRI_RU = {TriState.YES: "да", TriState.NO: "нет", TriState.UNKNOWN: "не указано"}
COMPONENT_RU = {
    "coverage": "покрытие каталогом",
    "margin": "маржа",
    "amount": "сумма",
    "payment": "оплата",
    "simplicity": "простота",
}
# (заголовок, ширина)
DEAL_COLUMNS = (
    ("N из 5", 7),
    ("Подача до (МСК)", 16),
    ("Осталось, ч", 9),
    ("Предмет (ссылка)", 45),
    ("Заказчик", 30),
    ("Площадка", 14),
    ("Способ", 18),
    ("НМЦ, ₽", 13),
    ("По каталогу, ₽", 13),
    ("Позиции в каталоге", 10),
    ("Оплата", 16),
    ("Договор", 18),
    ("ТЗ во вложении", 9),
    ("Только МСП", 9),
    ("Мин. срок КП, дн.", 9),
    ("Риски", 55),
    ("Почему", 75),
    ("Приоритет", 30),
    ("КП", 30),
    ("Документы", 9),
    ("Найдено (МСК)", 16),
    ("Статус", 14),
)


def _msk(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(MSK).replace(tzinfo=None, microsecond=0)


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _header(ws: Worksheet, columns: tuple[tuple[str, int], ...]) -> None:
    ws.append([c for c, _ in columns])
    for i, (_, width) in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
        cell = ws.cell(row=1, column=i)
        cell.font = HEAD_FONT
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 2 * LINE_PT + 4
    ws.freeze_panes = "A2"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"


def _fit_row(ws: Worksheet, row: int, widths: tuple[int, ...]) -> None:
    """Высота строки по самому длинному тексту с переносом: ~1,1 знака на единицу ширины колонки."""
    lines = 1
    for col, width in enumerate(widths, 1):
        value = ws.cell(row=row, column=col).value
        if isinstance(value, str) and value:
            per_line = max(int(width * 1.1), 1)
            lines = max(lines, sum(math.ceil(max(len(part), 1) / per_line) for part in value.split("\n")))
    ws.row_dimensions[row].height = min(lines * LINE_PT + 3, MAX_ROW_PT)


@dataclass(frozen=True)
class Period:
    """Сделки, найденные в [start, end) по МСК; include_open — плюс все, где приём КП ещё идёт."""

    start: datetime | None
    end: datetime | None
    include_open: bool
    title: str

    def where(self, now: datetime) -> ColumnElement[bool]:
        parts: list[ColumnElement[bool]] = []
        if self.start is not None:
            parts.append(Deal.created_at >= self.start)
        if self.end is not None:
            parts.append(Deal.created_at < self.end)
        found = and_(true(), *parts)
        return or_(found, Deal.end_at > now) if self.include_open else found


def day_period(first: date, last: date) -> Period:
    start = datetime.combine(first, time.min, MSK)
    end = datetime.combine(last + timedelta(days=1), time.min, MSK)
    title = f"{first:%d.%m.%Y}" if first == last else f"{first:%d.%m.%Y}–{last:%d.%m.%Y}"
    return Period(start, end, False, f"найдены {title}")


ALL_TIME = Period(None, None, False, "вся история")


def worked_filter() -> ColumnElement[bool]:
    """Разобранные моделью: рабочие статусы + истёкшие, у которых есть разбор."""
    return or_(
        Deal.status.in_(SHOWN),
        and_(Deal.status == DealStatus.EXPIRED, Deal.score_breakdown.has_key(ANALYZED_KEY)),
    )


def excluded_filter() -> ColumnElement[bool]:
    return or_(
        Deal.status.in_((DealStatus.FILTERED_OUT, DealStatus.NEW)),
        and_(Deal.status == DealStatus.EXPIRED, ~Deal.score_breakdown.has_key(ANALYZED_KEY)),
    )


@dataclass(frozen=True)
class ReportStats:
    deals: int
    excluded: int


def _payment(deal: Deal) -> str:
    text = PAYMENT_RU[deal.payment_terms]
    if deal.payment_days and deal.payment_terms is not PaymentTerms.PREPAYMENT:
        return f"{text} {deal.payment_days} дн."
    return text


def _priority(score: dict[str, Any]) -> str:
    comps = score.get("components") or {}
    parts = ", ".join(f"{COMPONENT_RU.get(k, k)} {v:.2f}" for k, v in comps.items())
    return f"{score.get('score', 0):.2f} ({parts})" if comps else ""


def _why(deal: Deal, score: dict[str, Any]) -> str:
    expl = deal.score_breakdown.get("explanation") or {}
    lines = list(expl.get("why") or [])
    lines += [f"{'✔' if c['met'] else '✘'} {c['title']}: {c['reason']}" for c in score.get("conditions", [])]
    if expl.get("check"):
        lines.append(f"Проверить: {expl['check']}")
    return "\n".join(lines)


def _kp(deal: Deal, proposal: Proposal | None) -> str:
    if proposal is not None and proposal.file_path:
        return f"есть, v{proposal.version}: {proposal.file_path.rsplit('/', 1)[-1]}"
    blockers = deal.score_breakdown.get("kp_blockers") or []
    return "нет: " + "; ".join(blockers[:3]) if blockers else "нет"


def build_report(session: Session, rules: Rules, now: datetime, period: Period) -> tuple[bytes, ReportStats]:
    """Сделки за период; статусы — на момент сборки."""
    platforms = {p.id: p for p in session.scalars(select(Platform))}
    deals = session.scalars(select(Deal).where(worked_filter(), period.where(now))).all()
    deals = sorted(
        deals,
        key=lambda d: (
            -(d.conditions_met or 0),
            d.end_at or datetime.max.replace(tzinfo=UTC),
            -float(d.score or 0),
        ),
    )
    ids = [d.id for d in deals]
    findings: dict[int, list[str]] = {}
    for f in session.scalars(select(Finding).where(Finding.deal_id.in_(ids)).order_by(Finding.id)):
        findings.setdefault(f.deal_id, []).append(f.text)
    proposals: dict[int, Proposal] = {}
    for p in session.scalars(select(Proposal).where(Proposal.deal_id.in_(ids)).order_by(Proposal.version)):
        proposals[p.deal_id] = p

    wb = Workbook()
    ws = wb.worksheets[0]
    ws.title = "Сделки"
    _header(ws, DEAL_COLUMNS)
    for deal in deals:
        score = deal.score_breakdown.get("score") or {}
        details = deal.raw_masked.get("details") or {}
        files = details.get("files") or []
        hours = None if deal.end_at is None else round((deal.end_at - now).total_seconds() / 3600, 1)
        hours = hours if hours is not None and hours > 0 else None
        catalog_total = score.get("catalog_total")
        coverage = (score.get("components") or {}).get("coverage")
        ws.append(
            [
                f"{deal.conditions_met} из 5" if deal.conditions_met is not None else "нужен человек",
                _msk(deal.end_at),
                hours,
                deal.title,
                deal.customer_name or "не указан",
                platforms[deal.platform_id].name,
                PROCEDURE_RU[deal.procedure],
                _num(deal.nmck),
                float(catalog_total) if catalog_total is not None else None,
                coverage,
                _payment(deal),
                CONTRACT_RU[deal.contract_format],
                TRI_RU[deal.has_tz],
                TRI_RU[deal.smb_only],
                deal.kp_min_valid_days,
                "\n".join(findings.get(deal.id, [])),
                _why(deal, score),
                _priority(score),
                _kp(deal, proposals.get(deal.id)),
                len(files) if isinstance(files, list) else 0,
                _msk(deal.created_at),
                STATUS_RU[deal.status],
            ]
        )
        row = ws.max_row
        ws.cell(row=row, column=4).hyperlink = deal.url
        ws.cell(row=row, column=4).font = Font(color="0563C1", underline="single")
        for col in (2, 21):
            ws.cell(row=row, column=col).number_format = DATETIME
        for col in (8, 9):
            ws.cell(row=row, column=col).number_format = MONEY
        ws.cell(row=row, column=10).number_format = "0%"
        if catalog_total is None and score.get("positions_without_price"):
            ws.cell(row=row, column=9).fill = BLANK_FILL
        if deal.status is DealStatus.HOT:
            ws.cell(row=row, column=1).fill = HOT_FILL
        for col in range(1, len(DEAL_COLUMNS) + 1):
            ws.cell(row=row, column=col).alignment = WRAP
        _fit_row(ws, row, tuple(w for _, w in DEAL_COLUMNS))
    ws.auto_filter.ref = ws.dimensions

    excluded = _excluded(wb.create_sheet("Исключённые"), session, platforms, now, period)
    _how(wb.create_sheet("Как считали"), rules, period, now)

    wb.properties.creator = "Закупщик"
    wb.properties.keywords = BUILD_MARK
    wb.properties.title = f"Сделки ({period.title}), выгружено {now.astimezone(MSK):%d.%m.%Y %H:%M} МСК"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), ReportStats(len(deals), excluded)


def _excluded(
    ws: Worksheet, session: Session, platforms: dict[int, Platform], now: datetime, period: Period
) -> int:
    columns = (
        ("Предмет (ссылка)", 50),
        ("Заказчик", 30),
        ("Площадка", 14),
        ("НМЦ, ₽", 13),
        ("Подача до (МСК)", 16),
        ("Причина", 50),
        ("Найдено (МСК)", 16),
    )
    _header(ws, columns)
    rows = session.scalars(
        select(Deal).where(excluded_filter(), period.where(now)).order_by(Deal.created_at, Deal.id)
    ).all()
    for deal in rows:
        reason = {
            DealStatus.FILTERED_OUT: deal.filter_reason,
            DealStatus.EXPIRED: deal.filter_reason or "приём закончился до разбора",
        }.get(deal.status, "ещё в очереди на разбор")
        ws.append(
            [
                deal.title,
                deal.customer_name or "не указан",
                platforms[deal.platform_id].name,
                _num(deal.nmck),
                _msk(deal.end_at),
                reason or "не указана",
                _msk(deal.created_at),
            ]
        )
        r = ws.max_row
        ws.cell(row=r, column=1).hyperlink = deal.url
        ws.cell(row=r, column=1).font = Font(color="0563C1", underline="single")
        ws.cell(row=r, column=4).number_format = MONEY
        for col in (5, 7):
            ws.cell(row=r, column=col).number_format = DATETIME
        for col in range(1, len(columns) + 1):
            ws.cell(row=r, column=col).alignment = WRAP
        _fit_row(ws, r, tuple(w for _, w in columns))
    ws.auto_filter.ref = ws.dimensions
    return len(rows)


def _how(ws: Worksheet, rules: Rules, period: Period, now: datetime) -> None:
    p = rules.profile
    f, c, w, kp = p.filters, p.conditions, p.weights, p.kp
    lines = [
        ("Период", period.title + (" и все, где ещё идёт приём КП" if period.include_open else "")),
        ("Выгружено", f"{now.astimezone(MSK):%d.%m.%Y %H:%M} МСК"),
        ("Версия правил", rules.version),
        ("Где правятся", "Стенд → «Админка»: каждое сохранение — новая версия с историей и откатом"),
        ("", ""),
        ("Отсев (лист «Исключённые»)", ""),
        ("НМЦ", f"до {f.max_nmck_rub:,.0f} ₽".replace(",", " ")),
        ("Берём способы", ", ".join(PROCEDURE_RU[x] for x in f.procedures_take)),
        ("Отсекаем способы", ", ".join(PROCEDURE_RU[x] for x in f.procedures_cut)),
        ("До конца подачи", f"не меньше {f.min_hours_left:g} ч"),
        ("Только МСП", "отсекаем, если в тексте: " + "; ".join(f.smb_only_phrases)),
        ("", ""),
        ("«N из 5» — пять условий", ""),
        ("1. Каталог", "все позиции найдены в каталоге"),
        ("2. Сумма", f"НМЦ не больше {f.max_nmck_rub:,.0f} ₽".replace(",", " ")),
        (
            "3. Упрощённая закупка",
            "только КП с ценой и сроком: без конкурса, обеспечения, лицензий участника, МСП",
        ),
        ("4. Оплата", f"предоплата или отсрочка не больше {c.max_postpay_days} дн."),
        ("5. Успеваем", f"до конца подачи не меньше {c.min_hours_for_kp:g} ч"),
        ("Горячая сделка", "5 из 5 и нет конфликта («эквивалент не допускается» при аналоге и т.п.)"),
        ("", ""),
        ("Приоритет внутри группы", "сумма слагаемых от 0 до 1 с весами"),
        *[(COMPONENT_RU[k], f"вес {v:g}") for k, v in w.model_dump().items()],
        ("", ""),
        ("КП", "только если все позиции совпали с каталогом точно, нет конфликта и «только МСП»"),
        ("Цены в КП", "из каталога на дату; нет цены — пустая ячейка с заливкой, ставит менеджер"),
        ("НДС", f"{kp.vat_rate:g}% (в цене); ПО из реестра Минцифры — без НДС"),
        ("Срок действия КП", f"{kp.valid_days} дн. или больше, если требует закупка"),
        ("", ""),
        ("Кто решает", "Робот КП не отправляет и заявку не подаёт: решение и подпись — за менеджером"),
    ]
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 100
    for key, value in lines:
        ws.append([key, value])
        if key and not value:
            ws.cell(row=ws.max_row, column=1).font = HEAD_FONT
        ws.cell(row=ws.max_row, column=2).alignment = WRAP
        _fit_row(ws, ws.max_row, (30, 100))
