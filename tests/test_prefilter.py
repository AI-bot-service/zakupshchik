from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.catalog.vendors import load_vendors
from app.db.models import Law, Procedure, TriState
from app.pipeline.prefilter import DealFacts, check
from app.scoring.rules import RulesError, load_rules

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules(ROOT / "config" / "rules.yaml")
VENDORS = load_vendors(ROOT / "config" / "vendors.yaml")
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def facts(title: str, **kw: object) -> DealFacts:
    base: dict[str, object] = {
        "title": title,
        "nmck": Decimal("120000"),
        "procedure": Procedure.SMALL_PURCHASE,
        "law": Law.COMMERCIAL,
        "end_at": NOW + timedelta(days=2),
        "smb_only": TriState.NO,
    }
    return DealFacts(**(base | kw))  # type: ignore[arg-type]


def test_hot_kaspersky_passes() -> None:
    v = check(
        facts("Продление лицензий Kaspersky Endpoint Security для бизнеса, 50 узлов"), RULES, VENDORS, NOW
    )
    assert v.passed and "kaspersky" in v.vendors


def test_vendor_beats_stop_word() -> None:
    v = check(facts("Права использования ПО «Кибер Бэкап» с технической поддержкой"), RULES, VENDORS, NOW)
    assert v.passed and "cyberprotect" in v.vendors


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Поставка мясной продукции", "не ПО"),
        ("Сопровождение программ для ЭВМ КонсультантПлюс", "не товар из каталога"),
        ("Абонентское обслуживание программного обеспечения навигационной системы", "не товар из каталога"),
    ],
)
def test_not_our_product_cut(title: str, reason: str) -> None:
    v = check(facts(title), RULES, VENDORS, NOW)
    assert not v.passed and v.cut[0].startswith(reason)


def test_competitive_procedure_and_big_sum_cut_with_all_reasons() -> None:
    v = check(
        facts("Лицензии Dr.Web", procedure=Procedure.TENDER, nmck=Decimal("16019743.11")), RULES, VENDORS, NOW
    )
    assert not v.passed
    assert any("конкурентная процедура" in c for c in v.cut)
    assert any("НМЦ 16 019 743 ₽" in c for c in v.cut)


def test_smb_only_cut_by_flag_and_phrase() -> None:
    assert not check(facts("Лицензии Dr.Web", smb_only=TriState.YES), RULES, VENDORS, NOW).passed
    v = check(
        facts("Лицензии Dr.Web (закупка среди субъектов малого предпринимательства)"), RULES, VENDORS, NOW
    )
    assert not v.passed and "МСП" in v.cut[0]


def test_time_rules() -> None:
    assert check(facts("Лицензии Dr.Web", end_at=NOW - timedelta(hours=1)), RULES, VENDORS, NOW).expired
    v = check(facts("Лицензии Dr.Web", end_at=NOW + timedelta(hours=1)), RULES, VENDORS, NOW)
    assert not v.passed and "до конца приёма" in v.cut[0]


def test_unknowns_are_risks_not_silent_cuts() -> None:
    v = check(
        facts("Лицензии Astra Linux", nmck=None, procedure=Procedure.UNKNOWN, law=Law.FZ44, end_at=None),
        RULES,
        VENDORS,
        NOW,
    )
    assert v.passed
    assert {"НМЦ не указана", "срок приёма заявок не указан"} <= set(v.risks)
    assert any("способ" in r for r in v.risks) and any("МСП" in r for r in v.risks)


@pytest.mark.parametrize(
    "title",
    [
        "Предоставление неисключительного права использования программы для ЭВМ «Куб CMS»",
        "Лицензия использования программного обеспечения Saby",
        "Передача неисключительных прав использования баз данных «Главная медицинская сестра»",
    ],
)
def test_foreign_products_cut(title: str) -> None:
    assert not check(facts(title), RULES, VENDORS, NOW).passed


def test_items_decide_when_title_is_a_category() -> None:
    title = (
        "ТОВАРЫ ИНФОРМАЦИОННО-ТЕХНОЛОГИЧЕСКИЕ, СРЕДСТВА СВЯЗИ, ОРГТЕХНИКА (ВКЛЮЧАЯ ПРОГРАММНОЕ ОБЕСПЕЧЕНИЕ)"
    )
    hardware = check(
        facts(title, items=("Монитор Asus VZ279HG 27", "ИБП Ippon Smart Power")), RULES, VENDORS, NOW
    )
    assert not hardware.passed and "в позициях" in hardware.cut[0]
    software = check(
        facts(title, items=("Расширение лицензии на Dr.Web Enterprise Security Suite",)), RULES, VENDORS, NOW
    )
    assert software.passed and "drweb" in software.vendors
    generic = check(
        facts("Лицензия Kaspersky Endpoint Security", items=("Права на программы",)), RULES, VENDORS, NOW
    )
    assert generic.passed


def test_vendor_words_do_not_match_inside_words() -> None:
    assert VENDORS.find("Поставка в г. Астрахань, Брестская обл.") == {}
    assert check(facts("Закупка 113520 бумаги"), RULES, VENDORS, NOW).cut


def test_rules_version_is_file_hash_and_bad_file_rejected(tmp_path: Path) -> None:
    assert len(RULES.version) == 12
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        (ROOT / "config" / "rules.yaml").read_text().replace("max_nmck_rub: 1000000", "max_nmck_rub: много")
    )
    with pytest.raises(RulesError, match="max_nmck_rub"):
        load_rules(bad)
