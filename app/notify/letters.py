"""Тексты писем менеджеру (§7.3): по горячей сделке и ежедневный Excel. Только plain text — доходит везде.

Робот КП заказчику не отправляет: письмо уходит менеджеру, решение и подпись — за ним.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.agent.schemas import Analysis
from app.db.models import CatalogItem, Deal
from app.pipeline.prefilter import PROCEDURE_RU
from app.report.excel import CONTRACT_RU, MSK, PAYMENT_RU, TRI_RU
from app.scoring.score import catalog_met


def rub(value: Decimal | None) -> str:
    return "не указана" if value is None else f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def short_rub(value: Decimal | None) -> str:
    if value is None:
        return "сумма не указана"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽".replace(".", ",")
    return f"{value / 1000:.0f} тыс. ₽"


def left(deal: Deal, now: datetime) -> str:
    if deal.end_at is None:
        return "срок подачи не указан"
    hours = (deal.end_at - now).total_seconds() / 3600
    when = deal.end_at.astimezone(MSK).strftime("%d.%m %H:%M")
    return f"до {when} МСК (осталось {hours:.0f} ч)" if hours >= 1 else f"до {when} МСК (меньше часа)"


@dataclass(frozen=True)
class DealLetter:
    deal: Deal
    platform: str
    analysis: Analysis
    items: dict[int, CatalogItem]
    findings: list[str]
    kp_files: list[str]
    docs_attached: list[str]
    docs_not_attached: list[str]
    card_url: str


def deal_subject(deal: Deal, analysis: Analysis, now: datetime) -> str:
    end = deal.end_at.astimezone(MSK).strftime("%d.%m %H:%M") if deal.end_at else "срок не указан"
    subject = analysis.subject if len(analysis.subject) <= 60 else analysis.subject[:57] + "…"
    mark = "" if catalog_met(deal.score_breakdown) else "нет в каталоге · "
    return f"{mark}{deal.conditions_met} из 5 · {subject} · {short_rub(deal.nmck)} · подача до {end}"


def deal_text(letter: DealLetter, now: datetime) -> str:
    d, a = letter.deal, letter.analysis
    score: dict[str, Any] = d.score_breakdown.get("score") or {}
    expl: dict[str, Any] = d.score_breakdown.get("explanation") or {}
    out = [
        f"{d.conditions_met} из 5 — {a.subject}",
        "",
        *(
            []
            if catalog_met(d.score_breakdown)
            else [
                "Нет в каталоге Софтлайна: решите, искать ли продукт у дистрибьютора или не идти. "
                "КП робот не сформировал.",
                "",
            ]
        ),
        f"Заказчик: {d.customer_name or 'не указан'}",
        f"Площадка: {letter.platform}, {PROCEDURE_RU[d.procedure]}",
        f"НМЦ: {rub(d.nmck)}",
        f"Подача КП: {left(d, now)}",
        "",
        "Позиции и каталог:",
    ]
    for i, p in enumerate(a.positions, 1):
        qty = f"{p.qty:g} {p.unit or ''}".strip() if p.qty else "кол-во не указано"
        item = letter.items.get(p.catalog_item_id or -1)
        if item is None:
            match = "нет в каталоге"
        else:
            price = rub(item.price) if item.price is not None else "нет цены — запросить у вендора"
            kind = "точно" if p.match.value == "exact" else "аналог"
            match = f"{kind}: {item.sku} · {item.name} · {price}"
        out.append(f"  {i}. {p.name} — {qty}\n     {match}")
    out += ["", "Условия:"]
    out += [f"  {'✔' if c['met'] else '✘'} {c['title']}: {c['reason']}" for c in score.get("conditions", [])]
    pay = PAYMENT_RU[d.payment_terms] + (f" {d.payment_days} дн." if d.payment_days else "")
    out += [
        "",
        f"Оплата: {pay}",
        f"Договор: {CONTRACT_RU[d.contract_format]}",
        f"ТЗ во вложении: {TRI_RU[d.has_tz]}",
    ]
    if score.get("catalog_total"):
        out.append(f"По ценам каталога: {rub(Decimal(score['catalog_total']))} (НМЦ {rub(d.nmck)})")
    if letter.findings:
        out += ["", "Риски:"] + [f"  • {f}" for f in letter.findings]
    if expl.get("why"):
        out += ["", "Почему идти:"] + [f"  • {w}" for w in expl["why"]]
    if expl.get("check"):
        out.append(f"Проверить: {expl['check']}")
    out += [""]
    out.append("Разбор в Excel во вложении — та же строка, что в ежедневном отчёте.")
    if letter.kp_files:
        out.append("КП во вложении: " + ", ".join(letter.kp_files))
        out.append("Цены — по каталогу на сегодня, подтверждает менеджер. Подпись и печать — вручную.")
    else:
        blockers = d.score_breakdown.get("kp_blockers") or []
        out.append("КП не сформировано: " + ("; ".join(blockers) if blockers else "нет данных"))
    if letter.docs_attached:
        out.append("Документы закупки во вложении: " + ", ".join(letter.docs_attached))
    if letter.docs_not_attached:
        out.append("Не вложены (открыть на площадке): " + ", ".join(letter.docs_not_attached))
    out += [
        "",
        f"Карточка: {letter.card_url}",
        f"Оригинал на площадке: {d.url}",
        "",
        "Письмо собрал робот «Закупщик». Заявку он не подаёт и КП заказчику не отправляет.",
    ]
    return "\n".join(out)


def daily_text(top: list[Deal], total: int, excluded: int, now: datetime, list_url: str) -> str:
    out = [
        f"Сделки на {now.astimezone(MSK):%d.%m.%Y %H:%M} МСК: в отчёте {total}, исключено {excluded}.",
        "",
    ]
    if top:
        out.append("Первые по приоритету:")
        for d in top:
            out.append(f"  • {d.conditions_met} из 5 · {short_rub(d.nmck)} · {left(d, now)} · {d.title[:90]}")
            out.append(f"    {d.url}")
    else:
        out.append("Разобранных сделок с открытой подачей нет.")
    out += [
        "",
        "Во вложении два файла: сделки за вчера и сегодня (рабочий) и вся история с первого дня (history).",
        "Если стенд недоступен, последний файл history — полная копия данных на утро.",
        "На стенде:",
        list_url,
    ]
    return "\n".join(out)
