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
from app.report.excel import CONTRACT_RU, PAYMENT_RU
from app.report.stamps import MSK
from app.scoring.score import CONDITIONS_TOTAL, catalog_met

PAPER, SHEET, INK, MUTED, RULE = "#eef1f5", "#ffffff", "#18212e", "#5a6576", "#d5dbe3"
STAMP, STAMP_SOFT = "#4338ca", "#e6e5fb"
OK, OK_SOFT, DOWN, DOWN_SOFT, PRICE_EMPTY = "#1e7a4c", "#e3f3ea", "#b42318", "#fdecea", "#fff1c2"
INSET = "#f5f7fa"
FONT = "Manrope,'Segoe UI',Roboto,Arial,sans-serif"

SHORT = {
    "catalog": "Каталог",
    "amount": "Сумма",
    "simple": "Простая",
    "payment": "Оплата",
    "time": "Срок",
    "fits": "Цена",
}


def condition_text(c: dict[str, Any]) -> str:
    """Строка под плиткой условия (стенд и письмо). НМЦ и срок уже в шапке — у выполненных порог."""
    if c.get("met") and c.get("key") in ("amount", "time"):
        return str(c.get("title", ""))
    return str(c.get("reason", ""))


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _section(title: str, body: str) -> str:
    return (
        f'<tr><td style="padding:24px 24px 0">'
        f'<div style="font:700 15px/1.3 {FONT};color:{INK};padding-bottom:8px;'
        f'border-bottom:1px solid {RULE}">{_e(title)}</div>{body}</td></tr>'
    )


TILES_PER_ROW = 3  # шесть плиток в 640 px письма: 3 × 2, как карточка на узком экране


def _tiles(conditions: list[dict[str, Any]]) -> str:
    """Условия закупки — те же плитки, что в карточке на стенде: ✓/✗, условие, причина по этой сделке."""
    met = sum(1 for c in conditions if c.get("met"))
    width = f"{100 / TILES_PER_ROW:.2f}%"
    rows = []
    for i in range(0, len(conditions), TILES_PER_ROW):
        cells = []
        for c in conditions[i : i + TILES_PER_ROW]:
            ok = bool(c.get("met"))
            fg, bg, text = (OK, INSET, MUTED) if ok else (DOWN, DOWN_SOFT, INK)
            label = SHORT.get(str(c.get("key")), str(c.get("title", "")))
            cells.append(
                f'<td width="{width}" valign="top" style="background:{bg};border-top:3px solid {fg};'
                f'border-left:4px solid {SHEET};border-right:4px solid {SHEET};padding:10px 12px 12px">'
                f'<div style="font:800 14px/1.3 {FONT};color:{fg}">'
                f"{'✓' if ok else '✗'}&nbsp;{_e(label)}</div>"
                f'<div style="font:400 12px/1.4 {FONT};color:{text};padding-top:4px">'
                f"{_e(condition_text(c))}</div></td>"
            )
        cells += [f'<td width="{width}" style="border:0"></td>'] * (TILES_PER_ROW - len(cells))
        rows.append(f'<tr>{"".join(cells)}</tr><tr><td colspan="{TILES_PER_ROW}" height="8"></td></tr>')
    return (
        f'<tr><td style="padding:24px 24px 0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;border-bottom:1px solid {RULE}"><tr>'
        f'<td style="font:700 15px/1.3 {FONT};color:{INK};padding-bottom:8px">Условия закупки</td>'
        f'<td align="right" style="font:400 13px/1.3 {FONT};color:{MUTED};padding-bottom:8px">'
        f"Выполнено {met} из {CONDITIONS_TOTAL}</td></tr></table>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;table-layout:fixed;margin-top:8px">{"".join(rows)}</table>'
        "</td></tr>"
    )


def _button(href: str, label: str, primary: bool) -> str:
    style = (
        f"background:{STAMP};color:{SHEET};padding:14px 22px;border-radius:6px"
        if primary
        else f"color:{STAMP};padding:14px 0"
    )
    weight = 700 if primary else 600
    return (
        f'<a href="{_e(href)}" style="display:inline-block;{style};font:{weight} 15px/1 {FONT};'
        f'text-decoration:none;margin:0 16px 8px 0">{_e(label)}</a>'
    )


def _page(parts: list[str]) -> str:
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:{PAPER}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{PAPER}"><tr><td align="center" style="padding:8px 4px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:640px;background:{SHEET};border:1px solid {RULE};border-radius:6px">'
        f"{''.join(parts)}</table></td></tr></table></body></html>"
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
        f"border-bottom:1px solid {RULE};"
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
    take = d.conditions_met == CONDITIONS_TOTAL and in_catalog
    verdict = "Стоит взять" if take else "Нужно ваше решение"
    verdict_color = OK if take else STAMP

    hours = None if d.end_at is None else (d.end_at - now).total_seconds() / 3600
    if d.end_at is None:
        deadline, urgent = "срок не указан", False
    else:
        when = d.end_at.astimezone(MSK).strftime("%d.%m %H:%M")
        rest = "меньше часа" if hours is not None and hours < 1 else f"осталось {hours:.0f} ч"
        deadline, urgent = f"до {when} МСК, {rest}", hours is not None and hours < 24
    days = f", {d.payment_days} дн." if d.payment_days else ""
    pay = PAYMENT_RU.get(d.payment_terms, "не указана") + days

    parts: list[str] = [
        # Шапка: вывод, что покупают, полоса условий
        f'<tr><td style="padding:24px 24px 0">'
        f'<div style="font:400 13px/1.3 {FONT};color:{MUTED}">Робот «Закупщик» нашёл сделку на площадке '
        f"{_e(letter.platform)}</div>"
        f'<div style="font:800 26px/1.15 {FONT};color:{verdict_color};padding-top:12px">{verdict}'
        f'<span style="color:{INK};font-weight:700"> — {d.conditions_met} из 6</span></div>'
        f'<div style="font:600 17px/1.4 {FONT};color:{INK};padding-top:8px">{_e(a.subject)}</div>'
        "</td></tr>",
        _tiles(conditions),
    ]
    how = (
        "«Подготовить КП» внизу письма, робот соберёт КП с пустыми ценами в карточку сделки."
        if letter.kp_url
        else "«Беру в работу» в карточке, робот соберёт КП с пустыми ценами."
    )
    if not in_catalog:
        parts.append(
            f'<tr><td style="padding:16px 24px 0"><div style="background:{DOWN_SOFT};'
            f"border-left:4px solid {DOWN};"
            f'padding:12px 16px;font:400 14px/1.45 {FONT};color:{INK}">'
            "<b>Позиции нет в каталоге Софтлайна.</b> "
            f"Решите, искать ли продукт у дистрибьютора или не идти. Если берёте — нажмите {how}"
            "</div></td></tr>"
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
    reasons = {str(c.get("reason")) for c in conditions if not c.get("met")}
    risks = [f for f in letter.findings if f not in reasons]
    if risks:
        parts.append(_section("Риски", _list(risks, DOWN)))
    if expl.get("why"):
        parts.append(_section("Пояснение робота", _list([str(w) for w in expl["why"]], STAMP)))
    if expl.get("check"):
        parts.append(
            f'<tr><td style="padding:16px 24px 0"><div style="background:{STAMP_SOFT};padding:12px 16px;'
            f'font:400 14px/1.45 {FONT};color:{INK}"><b>Проверьте:</b> {_e(expl["check"])}</div></td></tr>'
        )

    files = ["Разбор в Excel — та же строка, что в ежедневном отчёте"]
    if letter.kp_files:
        files.append(
            "КП: " + ", ".join(letter.kp_files) + ". Цены по каталогу на сегодня, подпись и печать вручную"
        )
    elif in_catalog:
        blockers = d.score_breakdown.get("kp_blockers") or []
        files.append(
            "КП не сформировано: "
            + ("; ".join(blockers) if blockers else "нет данных")
            + ". Нажмите "
            + ("«Подготовить КП»" if letter.kp_url else "«Беру в работу» в карточке")
            + " — робот соберёт КП с аналогами и пустыми ценами"
        )
    if letter.docs_attached:
        files.append("Документы закупки: " + ", ".join(letter.docs_attached))
    if letter.docs_not_attached:
        files.append("Не вложены, откройте на площадке: " + ", ".join(letter.docs_not_attached))
    parts.append(_section("Во вложении", _list(files, MUTED)))

    buttons = []
    if letter.kp_url:
        buttons.append(_button(letter.kp_url, "Подготовить КП", True))
    buttons += [
        _button(letter.card_url, "Открыть карточку", not letter.kp_url),
        _button(d.url, "Закупка на площадке", False),
    ]
    note = (
        "«Подготовить КП» берёт сделку в работу: робот соберёт КП, оно появится в карточке сделки. "
        if letter.kp_url
        else ""
    )
    parts.append(
        f'<tr><td style="padding:24px 24px 24px">{"".join(buttons)}'
        f'<div style="font:400 12px/1.5 {FONT};color:{MUTED};padding-top:16px">{note}'
        "Письмо собрал робот «Закупщик». "
        "Заявку он не подаёт и КП заказчику не отправляет — это делает менеджер.</div></td></tr>"
    )
    return _page(parts)
