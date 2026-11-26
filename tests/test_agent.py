import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from docx import Document

from app.agent.analyze import AgentContext, analyze_deal, card_text, verify
from app.agent.explain import Explanation, check_numbers, explain, fallback
from app.agent.journal import Journal
from app.agent.llm import LLMReply, Message, ToolCall
from app.agent.schemas import Analysis, analysis_parameters
from app.catalog.search import CatalogHit
from app.db.models import CatalogItem, Deal, DealItem, Law, MatchKind, Procedure, TriState
from app.pipeline.attachments import AttachmentError, AttachmentText, extract_text
from app.scoring.rules import load_rules
from app.scoring.score import DealView, evaluate

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules(ROOT / "config" / "rules.yaml")
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
TZ = (
    "Техническое задание. Антивирус Kaspersky Endpoint Security для бизнеса Стандартный, "
    "50 узлов, 12 месяцев."
)


def make_deal() -> Deal:
    deal = Deal(
        id=1,
        platform_id=1,
        external_id="x1",
        url="https://example.test/1",
        title="Поставка неисключительных прав на антивирусное ПО",
        customer_name="ГБОУ Школа № 1",
        nmck=Decimal("150000"),
        procedure=Procedure.SINGLE_SUPPLIER,
        procedure_raw="2",
        law=Law.FZ44,
        end_at=NOW + timedelta(days=3),
        smb_only=TriState.UNKNOWN,
        raw_masked={
            "details": {
                "payment_terms": "Оплата в течение 7 рабочих дней с даты подписания УПД",
                "files": [{"id": "77", "name": "ТЗ.docx"}],
            }
        },
    )
    deal.items = [DealItem(name="Программное обеспечение", qty=Decimal(1), unit="шт")]
    return deal


def catalog_item(item_id: int = 501, price: Decimal | None = Decimal("2100")) -> CatalogItem:
    return CatalogItem(
        id=item_id,
        product_id=item_id,
        sku="KL4863RAQFS",
        vendor="kaspersky",
        name="Kaspersky Endpoint Security для бизнеса Стандартный, 50-99 узлов, 1 год",
        price=price,
        url="https://store.example/kes",
        rf_registry_no="205",
    )


class FakeCatalog:
    def __init__(self) -> None:
        self.item = catalog_item()
        self.queries: list[str] = []

    def search(self, query: str, context: str) -> list[CatalogHit]:
        self.queries.append(query)
        if "kaspersky" not in query.lower():
            return []
        i = self.item
        return [
            CatalogHit(
                id=i.id, vendor=i.vendor, sku=i.sku, name=i.name, price=i.price, url=i.url, similarity=0.8
            )
        ]

    def get(self, item_id: int) -> CatalogItem | None:
        return self.item if item_id == self.item.id else None


def attachments(deal: Deal, file_id: str) -> AttachmentText:
    if file_id != "77":
        raise AttachmentError("нет такого файла")
    return AttachmentText(file_id, "ТЗ.docx", TZ, truncated=False, cached=False)


class ScriptedLLM:
    """Отдаёт заранее заданные ответы по очереди и запоминает, что ему прислали."""

    def __init__(self, replies: list[LLMReply]) -> None:
        self.replies = replies
        self.calls: list[list[Message]] = []

    def chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4000,
    ) -> LLMReply:
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0)


def reply(*calls: tuple[str, dict[str, Any]], content: str | None = None) -> LLMReply:
    return LLMReply(
        content=content,
        tool_calls=[
            ToolCall(id=f"c{i}", name=n, arguments=json.dumps(a, ensure_ascii=False))
            for i, (n, a) in enumerate(calls)
        ],
        model="fake",
        tokens_in=1000,
        tokens_out=100,
        cost_rub=Decimal("0.5"),
        cost_known=True,
        ms=10,
    )


QUOTE = "Kaspersky Endpoint Security для бизнеса Стандартный, 50 узлов"


def analysis_args(quote: str = QUOTE, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subject": "антивирус Kaspersky на 50 узлов",
        "is_license_supply": True,
        "not_license_reason": None,
        "positions": [
            {
                "name": "Kaspersky Endpoint Security Стандартный",
                "quote": quote,
                "qty": 50,
                "unit": "узел",
                "term_months": 12,
                "vendor": "Kaspersky",
                "product": "Endpoint Security для бизнеса Стандартный",
                "match": "exact",
                "catalog_item_id": 501,
                "confidence": 0.9,
                "reason": "тот же продукт и редакция",
            }
        ],
        "constraints": [],
        "payment_terms": "postpayment",
        "payment_days": 7,
        "payment_quote": "Оплата в течение 7 рабочих дней",
        "contract_format": "unknown",
        "delivery_days": None,
        "missing": [],
    }
    return base | kw


def ctx(llm: ScriptedLLM, catalog: FakeCatalog | None = None) -> AgentContext:
    return AgentContext(catalog or FakeCatalog(), attachments, llm, "fake-main", Journal(None, "run", 1))


# ── вложения ──────────────────────────────────────────────────────────────


def test_extract_docx_with_table_and_masking() -> None:
    doc = Document()
    doc.add_paragraph(
        "Описание объекта закупки. Контактное лицо Иванова Елена Петровна, тел. +7 (495) 123-45-67"
    )
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Dr.Web Desktop Security Suite"
    table.cell(0, 1).text = "25 лицензий"
    buf = io.BytesIO()
    doc.save(buf)
    text = extract_text("ТЗ.docx", buf.getvalue())
    assert "Dr.Web Desktop Security Suite | 25 лицензий" in text
    assert "123-45-67" not in text and "Иванова" not in text


def test_extract_old_word_doc_utf16_runs() -> None:
    body = "Предоставление эквивалента недопустимо. Контур-Экстерн, 1 лицензия".encode("utf-16le")
    data = b"\xd0\xcf\x11\xe0" + b"\x00" * 64 + body + b"\x00\x00\xff\xfe" * 8
    assert "эквивалента недопустимо" in extract_text("ООЗ.doc", data)


def test_extract_rejects_unknown_format_and_scans() -> None:
    with pytest.raises(AttachmentError, match="не читаем"):
        extract_text("чертёж.dwg", b"AC1027")
    with pytest.raises(AttachmentError):
        extract_text("скан.pdf", b"%PDF-1.4 broken")


# ── проверки ответа модели ────────────────────────────────────────────────


def test_tool_schema_has_no_refs() -> None:
    assert "$ref" not in json.dumps(analysis_parameters())


def test_verify_rejects_invented_quote_and_foreign_id() -> None:
    analysis = Analysis.model_validate(analysis_args(quote="Kaspersky Total Security на 100 узлов"))
    errors = verify(analysis, TZ + card_text(make_deal()), seen_ids=set())
    assert any("цитаты" in e for e in errors)
    assert any("не приходил из search_catalog" in e for e in errors)


def test_verify_accepts_quote_with_other_spacing_and_quotes() -> None:
    analysis = Analysis.model_validate(
        analysis_args(quote="«Kaspersky  Endpoint Security для бизнеса Стандартный», 50 узлов")
    )
    assert verify(analysis, TZ + card_text(make_deal()), seen_ids={501}) == []


# ── агентный цикл ─────────────────────────────────────────────────────────


def test_agent_opens_tz_searches_and_retries_after_rejection() -> None:
    llm = ScriptedLLM(
        [
            reply(("get_attachment_text", {"file_id": "77"})),
            reply(("search_catalog", {"query": "Endpoint Security Стандартный", "vendor": "Kaspersky"})),
            reply(("submit_analysis", analysis_args(quote="придуманная цитата про 100 узлов"))),
            reply(("submit_analysis", analysis_args())),
        ]
    )
    c = ctx(llm)
    res = analyze_deal(c, make_deal())
    assert res.analysis is not None and res.needs_human is None
    assert res.files_read == ["ТЗ.docx"] and res.searches == 1 and res.turns == 4
    assert res.cost_rub == Decimal("2.0")
    steps = [s["step"] for s in c.journal.steps]
    assert steps.count("agent_llm") == 4 and "agent_submit_rejected" in steps
    # Ошибка проверки ушла модели текстом, а текст закупки — в отдельном блоке данных
    last = llm.calls[-1]
    assert any(m["role"] == "tool" and "Разбор не принят" in m["content"] for m in last)
    assert last[1]["content"].startswith("<закупка>")


def test_agent_gives_up_after_two_bad_submissions() -> None:
    bad = analysis_args(quote="такого текста нет")
    llm = ScriptedLLM([reply(("submit_analysis", bad)), reply(("submit_analysis", bad))])
    res = analyze_deal(ctx(llm), make_deal())
    assert res.analysis is None and res.needs_human and "вручную" in res.needs_human


def test_agent_limits_searches() -> None:
    catalog = FakeCatalog()
    search = ("search_catalog", {"query": "Kaspersky"})
    final = analysis_args(quote="неисключительных прав на антивирусное ПО")
    llm = ScriptedLLM([reply(*[search] * 7), reply(("submit_analysis", final))])
    res = analyze_deal(ctx(llm, catalog), make_deal())
    assert len(catalog.queries) == 6 and res.searches == 6


# ── ограничения и «N из 5» ────────────────────────────────────────────────


def view(**kw: Any) -> DealView:
    base: dict[str, Any] = {
        "title": "x",
        "nmck": Decimal("150000"),
        "procedure": Procedure.SINGLE_SUPPLIER,
        "end_at": NOW + timedelta(days=3),
        "smb_only": TriState.UNKNOWN,
        "file_names": ("Проект контракта.docx",),
        "guarantee_flag": False,
    }
    return DealView(**(base | kw))


def test_five_of_five_is_hot() -> None:
    result = evaluate(view(), Analysis.model_validate(analysis_args()), {501: catalog_item()}, RULES, NOW)
    assert result.n == 5 and result.hot
    assert result.catalog_total == Decimal("105000")
    assert set(result.components) == {"coverage", "margin", "amount", "payment", "simplicity"}


def test_long_postpay_and_no_price() -> None:
    analysis = Analysis.model_validate(analysis_args(payment_days=30))
    result = evaluate(view(), analysis, {501: catalog_item(price=None)}, RULES, NOW)
    assert result.n == 4 and not result.hot
    assert any("нет цены" in f.text for f in result.findings)
    assert result.catalog_total is None and result.components["margin"] == 0.5


def test_no_equivalent_with_analog_is_conflict() -> None:
    pos = analysis_args()["positions"][0] | {"match": MatchKind.ANALOG.value}
    analysis = Analysis.model_validate(
        analysis_args(
            positions=[pos],
            constraints=[
                {"kind": "no_equivalent", "quote": "эквивалент не допускается", "note": "только этот"}
            ],
        )
    )
    result = evaluate(view(), analysis, {501: catalog_item()}, RULES, NOW)
    assert not result.hot and result.conditions[0].met is False


def test_services_not_license_supply() -> None:
    analysis = Analysis.model_validate(
        analysis_args(is_license_supply=False, not_license_reason="абонентское обслуживание", positions=[])
    )
    result = evaluate(view(), analysis, {}, RULES, NOW)
    assert result.conditions[0].met is False and "не поставка лицензий" in result.conditions[0].reason


# ── объяснение ────────────────────────────────────────────────────────────


def test_explanation_with_invented_number_falls_back() -> None:
    result = evaluate(view(), Analysis.model_validate(analysis_args()), {501: catalog_item()}, RULES, NOW)
    facts = {"НМЦ_руб": "150000", "условий_выполнено": "5 из 5"}
    invented = json.dumps({"why": ["Маржа 40%"], "check": "цены"}, ensure_ascii=False)
    llm = ScriptedLLM([reply(content=invented), reply(content=invented)])
    expl = explain(llm, "cheap", facts, result, Journal(None, "r", 1))
    assert expl.by_model is False and expl == fallback(result)


def test_explanation_numbers_checked_with_spaces() -> None:
    expl = Explanation(why=["НМЦ 150 000 ₽, 5 из 5"], check="Проверить цену")
    assert check_numbers(expl, '{"НМЦ_руб": "150000.00", "n": "5 из 5"}') == []
