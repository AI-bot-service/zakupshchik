"""HTML-версия письма по сделке. Таблицы и стили в атрибутах — так письмо одинаково выглядит в Яндекс Почте,
Outlook и Gmail. Цвета — из app/web/static/tokens.css. Всё, что пришло с площадки или от модели,
экранируется."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Any

from app.notify.letters import DealLetter, rub
from app.pipeline.prefilter import PROCEDURE_RU
from app.report.excel import CONTRACT_RU, MSK, PAYMENT_RU
from app.scoring.score import catalog_met

PAPER, SHEET, INK, MUTED, RULE = "#eef1f5", "#ffffff", "#18212e", "#5a6576", "#d5dbe3"
STAMP, STAMP_SOFT = "#4338ca", "#e6e5fb"
OK, OK_SOFT, DOWN, DOWN_SOFT, PRICE_EMPTY = "#1e7a4c", "#e3f3ea", "#b42318", "#fdecea", "#fff1c2"
FONT = "Manrope,'Segoe UI',Roboto,Arial,sans-serif"

SHORT = {
    "catalog": "Есть в каталоге",
    "amount": "До 1 млн ₽",
    "simple": "Простая",
    "payment": "Оплата",
    "time": "Успеваем",
}


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _section(title: str, body: str) -> str:
    return (
        f'<tr><td style="padding:24px 32px 0">'
        f'<div style="font:700 15px/1.3 {FONT};color:{INK};padding-bottom:8px;'
        f'border-bottom:1px solid {RULE}">{_e(title)}</div>{body}</td></tr>'
    )


def _bar(conditions: list[dict[str, Any]]) -> str:
    cells = []
    for c in conditions:
        met = bool(c.get("met"))
        fg, bg = (OK, OK_SOFT) if met else (DOWN, DOWN_SOFT)
        label = SHORT.get(str(c.get("key")), str(c.get("title", "")))
        cells.append(
            f'<td width="20%" style="background:{bg};color:{fg};padding:8px 4px;text-align:center;'
            f'font:700 12px/1.25 {FONT};border:2px solid {SHEET}">'
            f'<span style="font-size:15px">{"✔" if met else "✘"}</span><br>{_e(label)}</td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;margin-top:16px"><tr>{"".join(cells)}</tr></table>'
    )


def _facts(rows: list[tuple[str, str, bool, str | None]]) -> str:
    """(подпись, значение, жирное, цвет)."""
    out = []
    for label, value, strong, color in rows:
        weight = 700 if strong else 400
        out.append(
            f'<tr><td width="34%" style="padding:6px 12px 6px 0;font:400 14px/1.4 {FONT};color:{MUTED};'
            f'vertical-align:top">{_e(label)}</td>'
            f'<td style="padding:6px 0;font:{weight} 14px/1.4 {FONT};color:{color or INK}">{value}</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;margin-top:8px">{"".join(out)}</table>'
    )


def _list(items: list[str], color: str) -> str:
    li = "".join(
        f'<tr><td width="16" style="vertical-align:top;padding:4px 0;color:{color};'
        f'font:700 14px/1.45 {FONT}">•</td>'
        f'<td style="padding:4px 0;font:400 14px/1.45 {FONT};color:{INK}">{_e(t)}</td></tr>'
        for t in items
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin-top:8px">{li}</table>'
    )


def _positions(letter: DealLetter) -> str:
    head = "".join(
        f'<td width="{w}%" style="padding:8px;font:700 12px/1.3 {FONT};color:{MUTED};'
        f'border-bottom:1px solid {RULE};'
        f'text-align:{align}">{t}</td>'
        for t, align, w in (("Что просят", "left", 40), ("Что предлагаем", "left", 42), ("Цена", "right", 18))
    )
    rows = []
    for p in letter.analysis.positions:
        qty = f"{p.qty:g} {p.unit or ''}".strip() if p.qty else "кол-во не указано"
        item = letter.items.get(p.catalog_item_id or -1)
        price_cell = f'<td style="padding:8px;border-bottom:1px solid {RULE}"></td>'
        if item is None:
            offer = f'<span style="color:{DOWN};font-weight:700">Нет в каталоге</span>'
        else:
            exact = p.match.value == "exact"
            badge_fg, badge_bg = (OK, OK_SOFT) if exact else (STAMP, STAMP_SOFT)
            offer = (
                f'<span style="background:{badge_bg};color:{badge_fg};font-weight:700;padding:1px 6px;'
                f'border-radius:6px">{"точно" if exact else "аналог"}</span> '
                f'<span style="color:{MUTED}">{_e(item.sku)}</span><br>{_e(item.name)}'
            )
            if item.price is not None:
                price_cell = (
                    f'<td style="padding:8px;border-bottom:1px solid {RULE};text-align:right;'
                    "white-space:nowrap;"
                    f'font:700 14px/1.4 {FONT};color:{INK}">{_e(rub(item.price))}</td>'
                )
            else:
                price_cell = (
                    f'<td style="padding:8px;border-bottom:1px solid {RULE};background:{PRICE_EMPTY};'
                    f'text-align:right;font:400 12px/1.3 {FONT};color:{INK}">запросить у вендора</td>'
                )
        rows.append(
            f'<tr><td style="padding:8px;border-bottom:1px solid {RULE};vertical-align:top;'
            f'font:400 14px/1.4 {FONT};color:{INK}">{_e(p.name)}<br>'
            f'<span style="color:{MUTED};font-size:13px">{_e(qty)}</span></td>'
            f'<td style="padding:8px;border-bottom:1px solid {RULE};vertical-align:top;'
            f'font:400 14px/1.4 {FONT};color:{INK}">{offer}</td>{price_cell}</tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;margin-top:8px"><tr>{head}</tr>{"".join(rows)}</table>'
    )


def deal_html(letter: DealLetter, now: datetime) -> str:
    d, a = letter.deal, letter.analysis
    score: dict[str, Any] = d.score_breakdown.get("score") or {}
    expl: dict[str, Any] = d.score_breakdown.get("explanation") or {}
    conditions: list[dict[str, Any]] = score.get("conditions") or []
    in_catalog = catalog_met(d.score_breakdown)
    take = d.conditions_met == 5 and in_catalog
    verdict = "Стоит взять" if take else "Нужно ваше решение"
    verdict_color = OK if take else STAMP

    hours = None if d.end_at is None else (d.end_at - now).total_seconds() / 3600
    if d.end_at is None:
        deadline, urgent = "срок не указан", False
    else:
        when = d.end_at.astimezone(MSK).strftime("%d.%m %H:%M")
        rest = "меньше часа" if hours is not None and hours < 1 else f"осталось {hours:.0f} ч"
        deadline, urgent = f"до {when} МСК, {rest}", hours is not None and hours < 24
    pay = PAYMENT_RU.get(d.payment_terms, "не указана") + (f", {d.payment_days} дн." if d.payment_days else "")

    parts: list[str] = [
        # Шапка: вывод, что покупают, полоса условий
        f'<tr><td style="padding:28px 32px 0">'
        f'<div style="font:400 13px/1.3 {FONT};color:{MUTED}">Робот «Закупщик» нашёл сделку на площадке '
        f"{_e(letter.platform)}</div>"
        f'<div style="font:800 26px/1.15 {FONT};color:{verdict_color};padding-top:12px">{verdict}'
        f'<span style="color:{INK};font-weight:700"> — {d.conditions_met} из 5</span></div>'
        f'<div style="font:600 17px/1.4 {FONT};color:{INK};padding-top:8px">{_e(a.subject)}</div>'
        f"{_bar(conditions)}</td></tr>"
    ]
    if not in_catalog:
        parts.append(
            f'<tr><td style="padding:16px 32px 0"><div style="background:{DOWN_SOFT};'
            f"border-left:4px solid {DOWN};"
            f'padding:12px 16px;font:400 14px/1.45 {FONT};color:{INK}">'
            "<b>Позиции нет в каталоге Софтлайна.</b> "
            "Решите, искать ли продукт у дистрибьютора или не идти. КП робот не сформировал.</div></td></tr>"
        )
    facts = [
        ("НМЦ", _e(rub(d.nmck)), True, None),
        ("Подача КП", _e(deadline), True, DOWN if urgent else None),
        ("Заказчик", _e(d.customer_name or "не указан"), False, None),
        ("Площадка", _e(f"{letter.platform}, {PROCEDURE_RU[d.procedure]}"), False, None),
        ("Оплата", _e(pay), False, None),
        ("Договор", _e(CONTRACT_RU.get(d.contract_format, "не указан")), False, None),
    ]
    if score.get("catalog_total"):
        total = Decimal(score["catalog_total"])
        over = d.nmck is not None and total > d.nmck
        facts.append(("По ценам каталога", _e(rub(total)), over, DOWN if over else None))
    parts.append(_section("Главное", _facts(facts)))
    if a.positions:
        parts.append(_section("Что предлагаем", _positions(letter)))
    failed = [f"{c.get('title')}: {c.get('reason')}" for c in conditions if not c.get("met")]
    if failed:
        parts.append(_section("Что не выполнено", _list(failed, DOWN)))
    reasons = {str(c.get("reason")) for c in conditions if not c.get("met")}
    risks = [f for f in letter.findings if f not in reasons]
    if risks:
        parts.append(_section("Риски", _list(risks, DOWN)))
    if expl.get("why"):
        parts.append(_section("Пояснение робота", _list([str(w) for w in expl["why"]], STAMP)))
    if expl.get("check"):
        parts.append(
            f'<tr><td style="padding:16px 32px 0"><div style="background:{STAMP_SOFT};padding:12px 16px;'
            f'font:400 14px/1.45 {FONT};color:{INK}"><b>Проверьте:</b> {_e(expl["check"])}</div></td></tr>'
        )

    files = ["Разбор в Excel — та же строка, что в ежедневном отчёте"]
    if letter.kp_files:
        files.append(
            "КП: " + ", ".join(letter.kp_files) + ". Цены по каталогу на сегодня, подпись и печать вручную"
        )
    elif in_catalog:
        blockers = d.score_breakdown.get("kp_blockers") or []
        files.append("КП не сформировано: " + ("; ".join(blockers) if blockers else "нет данных"))
    if letter.docs_attached:
        files.append("Документы закупки: " + ", ".join(letter.docs_attached))
    if letter.docs_not_attached:
        files.append("Не вложены, откройте на площадке: " + ", ".join(letter.docs_not_attached))
    parts.append(_section("Во вложении", _list(files, MUTED)))

    parts.append(
        f'<tr><td style="padding:28px 32px 32px">'
        f'<a href="{_e(letter.card_url)}" style="display:inline-block;background:{STAMP};color:{SHEET};'
        f'font:700 15px/1 {FONT};text-decoration:none;padding:14px 22px;border-radius:6px">'
        "Открыть карточку</a>"
        f'<a href="{_e(d.url)}" style="display:inline-block;margin-left:16px;color:{STAMP};'
        f'font:600 14px/1 {FONT};padding:14px 0">Закупка на площадке</a>'
        f'<div style="font:400 12px/1.5 {FONT};color:{MUTED};padding-top:24px">'
        "Письмо собрал робот «Закупщик». "
        "Заявку он не подаёт и КП заказчику не отправляет — это делает менеджер.</div></td></tr>"
    )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:{PAPER}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{PAPER}"><tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:640px;background:{SHEET};border:1px solid {RULE};border-radius:6px">'
        f"{''.join(parts)}</table></td></tr></table></body></html>"
    )
