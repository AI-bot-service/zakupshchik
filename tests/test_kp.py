from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from docx import Document

from app.agent.schemas import Analysis
from app.db.models import FindingKind, TriState
from app.kp.draft import blockers, build_draft
from app.kp.render import BLANK_FILL, render_docx
from app.scoring.rules import load_rules
from app.scoring.score import FindingOut, ScoreResult
from tests.test_agent import NOW, analysis_args, catalog_item, make_deal

KP = load_rules().profile.kp


def score(*findings: FindingOut) -> ScoreResult:
    return ScoreResult([], list(findings), {}, 0.0, None, False)


def analysis(**kw: object) -> Analysis:
    return Analysis.model_validate(analysis_args(**kw))


def test_blockers_empty_for_exact_match() -> None:
    assert blockers(make_deal(), analysis(), score(), {501: catalog_item()}) == []


def test_blockers_analog_smb_conflict() -> None:
    pos = analysis_args()["positions"][0] | {"match": "analog"}
    deal = make_deal()
    deal.smb_only = TriState.YES
    why = blockers(
        deal,
        analysis(positions=[pos]),
        score(FindingOut(kind=FindingKind.CONFLICT, text="эквивалент не допускается")),
        {501: catalog_item()},
    )
    assert any("нет точного совпадения" in w for w in why)
    assert "только МСП" in why and any(w.startswith("конфликт") for w in why)


def test_draft_vat_free_registry_and_payment() -> None:
    draft = build_draft(make_deal(), analysis(), {501: catalog_item()}, KP, NOW.date())
    line = draft.lines[0]
    assert line.sku == "KL4863RAQFS" and line.qty == 50 and line.amount == Decimal("105000.00")
    assert line.vat_free and draft.vat_total is None
    assert draft.payment == "Постоплата 7 дней"
    assert draft.valid_until == NOW.date().replace(day=6, month=12)  # 10 дней по умолчанию


def test_draft_vat_included_and_min_validity() -> None:
    item = catalog_item()
    item.rf_registry_no = None
    deal = make_deal()
    deal.kp_min_valid_days = 30
    draft = build_draft(deal, analysis(payment_terms="unknown"), {501: item}, KP, NOW.date())
    assert draft.vat_total == Decimal("18934.43")  # 105000 × 22 / 122
    assert draft.payment == KP.default_payment
    assert (draft.valid_until - NOW.date()).days == 30


def test_render_blank_price_filled(tmp_path: Path) -> None:
    draft = build_draft(make_deal(), analysis(), {501: catalog_item(price=None)}, KP, NOW.date())
    priced = replace(draft.lines[0], sku="X-2", unit_price=Decimal("1000"), qty=Decimal(2), vat_free=False)
    draft = replace(draft, lines=[draft.lines[0], priced])
    path = render_docx(draft, tmp_path / "kp.docx")
    doc = Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Куда: ГБОУ Школа № 1" in doc.tables[0].rows[0].cells[1].text
    assert "Условия оплаты: Постоплата 7 дней." in text
    table = doc.tables[1]
    blank = table.rows[1].cells
    assert blank[0].text == "KL4863RAQFS" and blank[3].text == "" and blank[2].text == "50"
    assert BLANK_FILL in blank[3]._tc.xml and BLANK_FILL in blank[4]._tc.xml
    assert BLANK_FILL not in blank[5]._tc.xml  # «Без НДС» известно и без цены
    assert BLANK_FILL not in table.rows[2].cells[3]._tc.xml
    total = table.rows[3].cells
    assert "без позиций с пустой ценой" in total[1].text
    assert total[4].text == "2 000,00" and total[5].text == "360,66"
