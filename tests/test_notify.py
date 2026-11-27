from datetime import UTC, datetime
from email import message_from_bytes, policy

from app.agent.schemas import Analysis
from app.db.models import ContractFormat, PaymentTerms, TriState
from app.notify.letter_html import deal_html
from app.notify.letters import DealLetter, deal_subject, deal_text
from app.notify.postbox import Attachment, build_mime, sign_v4
from tests.test_agent import NOW, analysis_args, catalog_item, make_deal


def test_mime_with_cyrillic_attachment() -> None:
    raw = build_mime(
        "robot@mail.example",
        "m@example.test",
        "5 из 5 · Kaspersky",
        "Текст письма",
        [Attachment("КП_Need1_2026-11-26.pdf", b"%PDF-1.7 test", "application/pdf")],
    )
    msg = message_from_bytes(raw, policy=policy.default)
    assert msg["Subject"] == "5 из 5 · Kaspersky"
    parts = list(msg.iter_attachments())
    assert parts[0].get_filename() == "КП_Need1_2026-11-26.pdf" and parts[0].get_content() == b"%PDF-1.7 test"
    body = msg.get_body(("plain",))
    assert body is not None and "Текст письма" in body.get_content()


def test_sigv4_stable() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    a = sign_v4(b"{}", "KEYID", "secret", now)
    assert a == sign_v4(b"{}", "KEYID", "secret", now)
    assert a["X-Amz-Date"] == "20261126T120000Z"
    assert "Credential=KEYID/20261126/ru-central1/ses/aws4_request" in a["Authorization"]
    assert a != sign_v4(b"{ }", "KEYID", "secret", now)


def test_deal_letter() -> None:
    deal = make_deal()
    deal.conditions_met = 5
    deal.payment_terms, deal.payment_days = PaymentTerms.POSTPAYMENT, 7
    deal.contract_format, deal.has_tz = ContractFormat.NONE, TriState.YES
    deal.score_breakdown = {
        "score": {
            "conditions": [
                {"key": "catalog", "title": "Каталог", "met": True, "reason": "все позиции найдены"}
            ]
        },
        "explanation": {"why": ["предоплата не нужна"], "check": "редакцию"},
    }
    analysis = Analysis.model_validate(analysis_args())
    subject = deal_subject(deal, analysis, NOW)
    assert subject == "5 из 6 · антивирус Kaspersky на 50 узлов · 150 тыс. ₽ · подача до 29.11 15:00"
    letter = DealLetter(
        deal=deal,
        platform="Портал поставщиков",
        analysis=analysis,
        items={501: catalog_item()},
        findings=["постоплата 7 дней"],
        kp_files=["КП_x1.docx", "КП_x1.pdf"],
        docs_attached=["ТЗ.docx"],
        docs_not_attached=[],
        card_url="https://stand.example/deals/1",
    )
    text = deal_text(letter, NOW)
    assert "точно: KL4863RAQFS" in text and "2 100,00 ₽" in text
    assert "Нет в каталоге" not in text and "Разбор в Excel во вложении" in text
    deal.score_breakdown["score"]["conditions"][0]["met"] = False
    assert deal_subject(deal, analysis, NOW).startswith("нет в каталоге · 5 из 6")
    assert "Нет в каталоге Софтлайна" in deal_text(letter, NOW)
    assert "✔ Каталог: все позиции найдены" in text
    assert "Оплата: постоплата 7 дн." in text and "Риски:\n  • постоплата 7 дней" in text
    assert "КП во вложении: КП_x1.docx, КП_x1.pdf" in text and "подтверждает менеджер" in text
    assert "https://stand.example/deals/1" in text and deal.url in text
    assert "осталось 72 ч" in text


def test_deal_html_escapes_platform_text_and_has_text_fallback() -> None:
    deal = make_deal()
    deal.conditions_met = 5
    deal.conditions_met = 6
    deal.customer_name = '<script>alert(1)</script> "Школа"'
    deal.score_breakdown = {
        "score": {"conditions": [{"key": "catalog", "title": "Каталог", "met": True, "reason": "найдены"}]},
        "explanation": {"why": ["<b>жирно</b>"], "check": "редакцию"},
    }
    analysis = Analysis.model_validate(analysis_args())
    letter = DealLetter(
        deal=deal,
        platform="Портал поставщиков",
        analysis=analysis,
        items={501: catalog_item()},
        findings=[],
        kp_files=[],
        docs_attached=[],
        docs_not_attached=[],
        card_url="https://stand.example/deals/1",
    )
    html = deal_html(letter, NOW)
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;жирно" in html
    assert (
        "Стоит взять" in html
        and "Условия отбора" in html
        and "нашлись в каталоге" in html
        and "KL4863RAQFS" in html
    )
    assert 'href="https://stand.example/deals/1"' in html

    msg = message_from_bytes(build_mime("a@x.ru", "b@x.ru", "Тема", "текст", [], html), policy=policy.default)
    types = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in types and "text/html" in types
