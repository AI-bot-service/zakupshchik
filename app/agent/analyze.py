"""Агент разбора одной закупки (AGENT.md S4–S5): сам решает, открыть ли ТЗ и как искать в каталоге.

Инструменты только читают. Ответ модели принимает код: цитаты должны дословно найтись в тексте,
id товара — только
из результатов поиска этого разбора. Не прошло — один повтор с текстом ошибки, дальше «нужен человек».
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.config import PlatformConfig
from app.adapters.fetch import Fetcher
from app.agent.journal import Journal
from app.agent.llm import LLM, LLMReply, Message, ToolCall
from app.agent.schemas import Analysis, ConstraintKind, analysis_parameters
from app.catalog.search import CatalogHit, search_catalog
from app.catalog.vendors import VendorIndex
from app.db.models import CatalogItem, Deal, MatchKind
from app.pipeline.attachments import AttachmentError, AttachmentText, get_attachment_text
from app.pipeline.prefilter import PROCEDURE_RU

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
MAX_TURNS = 12
MAX_SEARCHES = 6
MAX_FILES = 5
# Файлы закупки кладутся в разбор сразу, а не по выбору модели: иначе итог зависит от того, какие 2 из 3
# она открыла (пропущен проект контракта — не видны оплата и обеспечение). Не влезло — модель откроет сама.
PRELOAD_CHARS = 90_000
MAX_SUBMIT_ERRORS = 2
MAX_COST_RUB = Decimal("20")


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    version: str


def load_prompt(name: str) -> Prompt:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return Prompt(name, text, f"{name}-{hashlib.sha256(text.encode()).hexdigest()[:8]}")


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_attachment_text",
            "description": (
                "Текст файла закупки (ТЗ, спецификация, проект контракта) по id из списка файлов карточки."
            ),
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
                "required": ["file_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": "Поиск товара в каталоге Софтлайн: до 8 совпадений с id, артикулом и ценой",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Продукт и редакция, можно с вендором"},
                    "vendor": {"type": "string", "description": "Вендор, если известен"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_catalog_item",
            "description": "Карточка товара каталога: редакция, тип и срок лицензии, количество, реестр РФ",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_analysis",
            "description": "Сдать итог разбора. Вызвать один раз в конце.",
            "parameters": analysis_parameters(),
        },
    },
]


def _money(value: Decimal | None) -> str:
    return "не указана" if value is None else f"{value:,.2f}".replace(",", " ") + " ₽"


def card_text(deal: Deal) -> str:
    """Карточка для модели: только поля сделки после маскирования ПДн."""
    lines = [
        f"Название: {deal.title}",
        f"Заказчик: {deal.customer_name or 'не указан'}",
        f"НМЦ: {_money(deal.nmck)}",
        f"Способ: {PROCEDURE_RU[deal.procedure]} ({deal.procedure_raw or '—'}), закон: {deal.law.value}",
        f"Приём предложений до: {deal.end_at.isoformat() if deal.end_at else 'не указано'}",
    ]
    if deal.items:
        lines.append("Позиции:")
        for i, item in enumerate(deal.items, 1):
            price = f", цена за ед. {_money(item.unit_price)}" if item.unit_price is not None else ""
            qty = (
                f"{item.qty.normalize():f} {item.unit or ''}".strip()
                if item.qty is not None
                else "кол-во не указано"
            )
            lines.append(f"  {i}. {item.name} — {qty}{price}")
    details = deal.raw_masked.get("details", {})
    for key, value in details.items():
        if key != "files" and value not in (None, "", [], {}):
            lines.append(
                f"{key}: {json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}"
            )
    files = details.get("files") or []
    if files:
        lines.append("Файлы закупки (№, id для get_attachment_text — имя):")
        lines.extend(
            f"  {n}. id {f['id']} — {f['name']}" for n, f in enumerate(files, 1) if isinstance(f, dict)
        )
    else:
        lines.append("Файлы закупки: нет в карточке")
    extra = {k: v for k, v in deal.raw_masked.items() if k != "details" and isinstance(v, str | int | float)}
    if extra:
        lines.append("Поля площадки: " + json.dumps(extra, ensure_ascii=False)[:1500])
    return "\n".join(lines)


_QUOTES = re.compile(r"[«»\"'„“”`]")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:)])")


def _norm(text: str) -> str:
    """Сравнение цитат: без регистра, кавычек и разницы в пробелах — модель часто их меняет."""
    flat = " ".join(_QUOTES.sub("", text.lower().replace("ё", "е")).split())
    return _SPACE_BEFORE_PUNCT.sub(r"\1", flat)


# «Обеспечение» по 44/223-ФЗ: деньги или банковская гарантия под заявку, исполнение контракта или гарантийные
# обязательства. Модель путает его с гарантийным сроком поставщика и с оборотом «для обеспечения нужд».
_GUARANTEE = re.compile(
    r"обеспечени\w*\s+(?:\S+\s+){0,3}?(?:заяв|исполнени|гарантийн|контракт|договор)"
    r"|размер\w*\s+обеспечени|банковск\w*\s+гаранти"
)
_GUARANTEE_ABSENT = re.compile(r"не\s+(?:установл|устанавлива|требу|предусм|предоставл)|отсутству")


def guarantee_error(quote: str) -> str | None:
    q = _norm(quote)
    if not _GUARANTEE.search(q):
        return (
            f"ограничение guarantee: в цитате «{quote[:80]}» нет обеспечения заявки или исполнения "
            "контракта. Гарантийный срок и гарантийные обязательства поставщика — не обеспечение: "
            "kind=other или не указывай"
        )
    if _GUARANTEE_ABSENT.search(q):
        return (
            f"ограничение guarantee: цитата «{quote[:80]}» говорит, что обеспечения нет, — "
            "не добавляй ограничение"
        )
    return None


def verify(analysis: Analysis, corpus: str, seen_ids: set[int]) -> list[str]:
    """Проверки кодом до использования ответа. Пустой список — принято."""
    norm = _norm(corpus)
    errors: list[str] = []
    quotes = [(f"позиция «{p.name[:40]}»", p.quote) for p in analysis.positions]
    quotes += [(f"ограничение {c.kind.value}", c.quote) for c in analysis.constraints]
    if analysis.payment_quote:
        quotes.append(("оплата", analysis.payment_quote))
    for where, quote in quotes:
        if _norm(quote) not in norm:
            errors.append(f"{where}: цитаты «{quote[:80]}» нет в тексте закупки — дай дословный фрагмент")
    for c in analysis.constraints:
        if c.kind is ConstraintKind.GUARANTEE and (err := guarantee_error(c.quote)):
            errors.append(err)
    for p in analysis.positions:
        if p.match is MatchKind.NONE and p.catalog_item_id is not None:
            errors.append(f"позиция «{p.name[:40]}»: при match=none catalog_item_id должен быть null")
        if p.match is not MatchKind.NONE:
            if p.catalog_item_id is None:
                errors.append(f"позиция «{p.name[:40]}»: при match={p.match.value} нужен catalog_item_id")
            elif p.catalog_item_id not in seen_ids:
                errors.append(
                    f"позиция «{p.name[:40]}»: id {p.catalog_item_id} не приходил из search_catalog — "
                    "найди товар поиском"
                )
    if analysis.is_license_supply and not analysis.positions:
        errors.append("поставка лицензий без позиций: перечисли позиции")
    if not analysis.is_license_supply and not analysis.not_license_reason:
        errors.append("is_license_supply=false: заполни not_license_reason")
    return errors


@dataclass
class AgentResult:
    analysis: Analysis | None
    needs_human: str | None
    prompt_version: str
    input_hash: str
    files_read: list[str] = field(default_factory=list)
    searches: int = 0
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: Decimal = Decimal(0)


class CatalogPort(Protocol):
    def search(self, query: str, context: str) -> list[CatalogHit]: ...
    def get(self, item_id: int) -> CatalogItem | None: ...


# (сделка, id файла) → текст; AttachmentError — файл не открыть
AttachmentPort = Callable[[Deal, str], AttachmentText]


class DbCatalog:
    def __init__(self, session: Session, vendors: VendorIndex) -> None:
        self._session = session
        self._vendors = vendors

    def search(self, query: str, context: str) -> list[CatalogHit]:
        return search_catalog(self._session, self._vendors, query, context=context, limit=8)

    def get(self, item_id: int) -> CatalogItem | None:
        return self._session.get(CatalogItem, item_id)


def platform_attachments(
    session: Session, fetcher: Fetcher | None, cfg: PlatformConfig | None
) -> AttachmentPort:
    def fetch(deal: Deal, file_id: str) -> AttachmentText:
        if fetcher is None or cfg is None:
            raise AttachmentError("файлы этой площадки без входа не скачиваются — открыть на площадке")
        return get_attachment_text(session, fetcher, cfg, deal, file_id)

    return fetch


@dataclass
class AgentContext:
    catalog: CatalogPort
    attachments: AttachmentPort
    llm: LLM
    model: str
    journal: Journal
    # Вендоры каталога: продукт другого вендора модель отмечает none без перебора формулировок поиска
    catalog_vendors: tuple[str, ...] = ()


def input_hash(deal: Deal, prompt: Prompt, model: str) -> str:
    return hashlib.sha256(f"{model}\n{prompt.version}\n{card_text(deal)}".encode()).hexdigest()


class _Run:
    def __init__(self, ctx: AgentContext, deal: Deal, prompt: Prompt) -> None:
        self.ctx = ctx
        self.deal = deal
        self.card = card_text(deal)
        self.corpus = [self.card]
        self.seen: set[int] = set()
        self.opened: set[str] = set()
        self.submit_errors = 0
        self.result = AgentResult(None, None, prompt.version, input_hash(deal, prompt, ctx.model))
        self.handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "get_attachment_text": self._attachment,
            "search_catalog": self._search,
            "get_catalog_item": self._item,
        }

    def _open(self, file_id: str) -> AttachmentText:
        att = self.ctx.attachments(self.deal, file_id)
        self.opened.add(file_id)
        self.result.files_read.append(att.name)
        self.corpus.append(att.text)
        return att

    @staticmethod
    def _wrap(att: AttachmentText) -> str:
        tail = "\n[…дальше обрезано]" if att.truncated else ""
        return f'<файл name="{att.name}">\n{att.text}{tail}\n</файл>'

    def _preload(self) -> str:
        """Все файлы закупки в первом сообщении, пока помещаются в PRELOAD_CHARS."""
        files = [f for f in self.deal.raw_masked.get("details", {}).get("files") or [] if isinstance(f, dict)]
        parts: list[str] = []
        left: list[str] = []
        budget = PRELOAD_CHARS
        for f in files[:MAX_FILES]:
            file_id, name = str(f.get("id", "")), str(f.get("name", ""))
            if budget <= 0:
                left.append(f"id {file_id} — {name}")
                continue
            try:
                att = self._open(file_id)
            except AttachmentError as exc:
                parts.append(f'<файл name="{name}">не открыт: {exc}</файл>')
                continue
            budget -= len(att.text)
            parts.append(self._wrap(att))
        left += [f"id {f.get('id')} — {f.get('name')}" for f in files[MAX_FILES:]]
        if left:
            parts.append("Не загружены (открой через get_attachment_text, если нужны): " + "; ".join(left))
        return "\n".join(parts)

    def _attachment(self, args: dict[str, Any]) -> str:
        file_id = str(args.get("file_id", ""))
        if file_id in self.opened:
            return "этот файл уже приложен к закупке выше — читай его там"
        if len(self.result.files_read) >= MAX_FILES:
            return f"лимит: открыто уже {MAX_FILES} файлов. Работай с тем, что есть."
        try:
            return self._wrap(self._open(file_id))
        except AttachmentError as exc:
            return f"файл не открыт: {exc}"

    def _search(self, args: dict[str, Any]) -> str:
        if self.result.searches >= MAX_SEARCHES:
            return "лимит: 6 поисков сделано. Сдавай разбор с тем, что нашёл."
        self.result.searches += 1
        query = str(args.get("query", ""))[:300]
        vendor = str(args.get("vendor") or "")[:100]
        hits = self.ctx.catalog.search(f"{vendor} {query}".strip(), self.deal.title)
        self.seen.update(h.id for h in hits)
        if not hits:
            return (
                "ничего не найдено: запрос слишком общий или такого продукта нет в каталоге. "
                "Уточни вендора и продукт (можно из ТЗ) или другое написание. Продукт вендора не из "
                "<вендоры_каталога> не ищи дальше — match=none."
            )
        return json.dumps(
            [
                {
                    "id": h.id,
                    "vendor": h.vendor,
                    "sku": h.sku,
                    "name": h.name,
                    "price_rub": str(h.price) if h.price is not None else None,
                    "similarity": h.similarity,
                }
                for h in hits
            ],
            ensure_ascii=False,
        )

    def _item(self, args: dict[str, Any]) -> str:
        raw = args.get("id")
        item_id = raw if isinstance(raw, int) else -1
        if item_id not in self.seen:
            return "этот id не приходил из search_catalog в этом разборе — сначала найди товар поиском"
        item = self.ctx.catalog.get(item_id)
        if item is None:
            return "товара нет в каталоге"
        return json.dumps(
            {
                "id": item.id,
                "vendor": item.vendor,
                "sku": item.sku,
                "name": item.name,
                "edition": item.edition,
                "license_type": item.license_type,
                "term": item.term,
                "client_type": item.client_type,
                "min_qty": item.min_qty,
                "max_qty": item.max_qty,
                "price_rub": str(item.price) if item.price is not None else None,
                "rf_registry_no": item.rf_registry_no,
                "delivery": item.delivery_days,
            },
            ensure_ascii=False,
        )

    def _submit(self, call: ToolCall) -> str | None:
        """None — принято; строка — ошибка для модели."""
        try:
            analysis = Analysis.model_validate_json(call.arguments)
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]]
        else:
            errors = verify(analysis, "\n".join(self.corpus), self.seen)
            if not errors:
                self.result.analysis = analysis
                return None
        self.submit_errors += 1
        self.ctx.journal.add("agent_submit_rejected", output={"errors": errors})
        return "Разбор не принят:\n- " + "\n- ".join(errors)

    def _account(self, reply: LLMReply) -> None:
        r = self.result
        r.turns += 1
        r.tokens_in += reply.tokens_in
        r.tokens_out += reply.tokens_out
        r.cost_rub += reply.cost_rub
        self.ctx.journal.add(
            "agent_llm",
            output={
                "turn": r.turns,
                "tool_calls": [c.name for c in reply.tool_calls],
                "cost_known": reply.cost_known,
            },
            model=reply.model,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
            cost_rub=reply.cost_rub,
            ms=reply.ms,
            input_hash=r.input_hash,
        )

    def run(self, prompt: Prompt) -> AgentResult:
        messages: list[Message] = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": f"<закупка>\n{self.card}\n</закупка>"},
        ]
        files = self._preload()
        if files:
            messages[1]["content"] += f"\n<файлы_закупки>\n{files}\n</файлы_закупки>"
        if self.ctx.catalog_vendors:
            vendors = "; ".join(self.ctx.catalog_vendors)
            messages[1]["content"] += f"\n<вендоры_каталога>{vendors}</вендоры_каталога>"
        while self.result.turns < MAX_TURNS:
            if self.result.cost_rub > MAX_COST_RUB:
                self.result.needs_human = f"бюджет разбора {MAX_COST_RUB} ₽ исчерпан"
                return self.result
            reply = self.ctx.llm.chat(self.ctx.model, messages, TOOLS, max_tokens=6000)
            self._account(reply)
            messages.append(reply.as_message())
            if not reply.tool_calls:
                messages.append({"role": "user", "content": "Заверши разбор вызовом submit_analysis."})
                continue
            for call in reply.tool_calls:
                content = self._handle(call)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if self.result.analysis is not None:
                    return self.result
                if self.submit_errors >= MAX_SUBMIT_ERRORS:
                    self.result.needs_human = "ответ модели дважды не прошёл проверку — разобрать вручную"
                    return self.result
        self.result.needs_human = f"агент не уложился в {MAX_TURNS} шагов"
        return self.result

    def _handle(self, call: ToolCall) -> str:
        if call.name == "submit_analysis":
            return self._submit(call) or "принято"
        handler = self.handlers.get(call.name)
        if handler is None:
            return f"нет инструмента {call.name}"
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return "аргументы не JSON"
        if not isinstance(args, dict):
            return "аргументы должны быть объектом"
        result = handler(args)
        self.ctx.journal.add(
            f"tool:{call.name}",
            output={"args": {k: str(v)[:200] for k, v in args.items()}, "result": result[:300]},
        )
        return result


def analyze_deal(ctx: AgentContext, deal: Deal, prompt: Prompt | None = None) -> AgentResult:
    prompt = prompt or load_prompt("analyze")
    return _Run(ctx, deal, prompt).run(prompt)
