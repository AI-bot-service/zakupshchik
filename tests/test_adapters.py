"""Контрактные тесты адаптеров: сохранённый ответ площадки → ожидаемые сделки."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.adapters import parse
from app.adapters.config import PlatformConfig, load_platforms
from app.adapters.details import details_url, parse_details
from app.adapters.extract import ExtractError
from app.adapters.fetch import Fetcher, build_params
from app.adapters.normalize import MSK, parse_dt, parse_money, resolve
from app.db.models import Law, Procedure, TriState

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
CONFIGS = {c.code: c for c in load_platforms(ROOT / "config" / "platforms.yaml")}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_mos_portal_contract() -> None:
    result = parse(fixture("mos_portal_query.json"), CONFIGS["mos_portal"])
    assert len(result.deals) == 6 and result.skipped == 0
    need = next(d for d in result.deals if d.external_id == "Need6322887")
    assert need.url == "https://zakupki.mos.ru/need/view?needId=6322887"
    assert need.procedure is Procedure.SINGLE_SUPPLIER
    assert need.law is Law.FZ44
    assert need.nmck == Decimal("23400.00")
    assert need.end_at == datetime(2026, 12, 1, 18, 0, tzinfo=MSK)
    assert need.customer_inn == "7124500810"
    auction = next(d for d in result.deals if d.external_id == "Auction10355601")
    assert auction.procedure is Procedure.QUOTATION_SESSION
    assert auction.url.endswith("auctionId=10355601")
    assert set(need.raw_masked) <= set(CONFIGS["mos_portal"].keep_raw)


def test_tektorg_contract() -> None:
    result = parse(fixture("tektorg_market.html"), CONFIGS["tektorg_market"])
    assert len(result.deals) == 6
    deal = next(d for d in result.deals if d.external_id == "20398475")
    assert deal.url == "https://www.tektorg.ru/market/procedures/20398475"
    assert deal.procedure is Procedure.SMALL_PURCHASE
    assert deal.nmck is None  # «НМЦ не установлена»
    assert deal.smb_only is TriState.NO
    assert deal.end_at is not None and deal.end_at.utcoffset() is not None
    with_price = next(d for d in result.deals if d.external_id == "20387590")
    assert with_price.nmck == Decimal("6896025.60")


def test_fabrikant_rsc_contract() -> None:
    result = parse(fixture("fabrikant_purchases.html"), CONFIGS["fabrikant"])
    ids = [d.external_id for d in result.deals]
    assert ids == ["680108597", "680067878", "680060960", "680026384", "679973920"]
    first = result.deals[0]
    assert first.url.startswith("https://fabrikant.ru/v2/trades/procedure/view/")
    assert first.procedure is Procedure.TENDER and first.law is Law.COMMERCIAL
    assert first.nmck == Decimal("375000.00")
    assert first.end_at == datetime(2026, 12, 11, 13, 0, tzinfo=MSK)
    assert first.customer_name == 'ООО "НОВАПОРТ-ИТ"'


def test_layout_change_is_reported_not_silent() -> None:
    with pytest.raises(ExtractError):
        parse("<html>новая вёрстка</html>", CONFIGS["tektorg_market"])
    with pytest.raises(ExtractError):
        parse('{"rows": []}', CONFIGS["mos_portal"])


def test_record_without_id_is_skipped() -> None:
    cfg = CONFIGS["mos_portal"]
    result = parse('{"items": [{"name": "без номера"}]}', cfg)
    assert result.deals == [] and result.skipped == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (23400.0, Decimal("23400.00")),
        ("6896025.6 ₽", Decimal("6896025.60")),
        ("1 330 000,00 RUB", Decimal("1330000.00")),
        ("НМЦ не установлена", None),
        (None, None),
        (True, None),
    ],
)
def test_parse_money(raw: object, expected: Decimal | None) -> None:
    assert parse_money(raw) == expected


def test_parse_dt_keeps_offset_and_defaults_to_msk() -> None:
    assert parse_dt("2026-11-26T13:30:00+03:00", []) == datetime(2026, 11, 26, 13, 30, tzinfo=MSK)
    assert parse_dt("11.12.2026 13:00", ["%d.%m.%Y %H:%M"]) == datetime(2026, 12, 11, 13, 0, tzinfo=MSK)
    assert parse_dt("когда-нибудь", ["%d.%m.%Y"]) is None


def test_resolve_template_fallback() -> None:
    spec = ["https://x.ru/need/{needId}", "https://x.ru/auction/{auctionId}"]
    assert resolve({"needId": None, "auctionId": 7}, spec) == "https://x.ru/auction/7"
    assert resolve({"a": {"b": [{"c": "ok"}]}}, "a.b.0.c") == "ok"
    assert resolve({}, "a.b") is None


def test_build_params_fills_query_everywhere() -> None:
    params = build_params(CONFIGS["mos_portal"], "антивирус")
    assert '"nameLike":{"value":"антивирус"}' in str(params["queryDto"])
    assert build_params(CONFIGS["fabrikant"], "ПО")["section_ids[]"] == ["2", "16"]


def test_fetcher_waits_between_requests_to_one_platform() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    cfg: PlatformConfig = CONFIGS["tektorg_market"]
    with Fetcher(None, clock=lambda: now[0], sleep=sleep) as f:
        f._wait_turn(cfg)
        now[0] += 1
        f._wait_turn(cfg)
    assert slept == [pytest.approx(cfg.min_interval_s - 1)]


def _flaky_fetcher(codes: list[int], monkeypatch: pytest.MonkeyPatch) -> tuple[Fetcher, list[int]]:
    seen: list[int] = []

    def reply(_: httpx.Request) -> httpx.Response:
        seen.append(codes[len(seen)])
        return httpx.Response(seen[-1], text="{}")

    monkeypatch.setattr(Fetcher._get.retry, "sleep", lambda _: None)
    f = Fetcher(None, sleep=lambda _: None)
    f._client = httpx.Client(transport=httpx.MockTransport(reply))
    return f, seen


def test_flaky_status_is_retried_only_where_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    portal = CONFIGS["mos_portal"]
    assert 400 in portal.retry_status
    f, seen = _flaky_fetcher([400, 200], monkeypatch)
    assert f.get_url(portal, portal.url) == "{}"
    assert seen == [400, 200]

    f, seen = _flaky_fetcher([400, 200], monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        f.get_url(CONFIGS["tektorg_market"], CONFIGS["tektorg_market"].url)
    assert seen == [400]


def test_platform_codes_unique_and_https() -> None:
    assert len(CONFIGS) >= 3
    assert all(c.url.startswith("https://") for c in CONFIGS.values())


def test_mos_portal_details_need_and_auction() -> None:
    spec = CONFIGS["mos_portal"].details
    assert spec is not None
    need = parse_details(fixture("mos_portal_need.json"), spec)
    assert len(need.items) == 2
    assert need.items[0].okpd and need.items[0].unit_price == Decimal("11747.13")
    assert need.extra["conclusion_reason"].startswith("п. 5 ч. 1 ст. 93")
    assert "payment_terms" not in need.extra  # «-» на площадке = не указано
    assert need.files and "Проект контракта" in need.files[0].name and need.files[0].id == "288445891"
    auction = parse_details(fixture("mos_portal_auction.json"), spec)
    assert len(auction.items) == 2 and auction.items[0].unit == "усл. ед"
    assert auction.extra["okpd_name"] == "Обеспечение программное сетевое на электронном носителе"


def test_details_never_copy_contacts() -> None:
    spec = CONFIGS["mos_portal"].details
    assert spec is not None
    text = str(parse_details(fixture("mos_portal_need.json"), spec).model_dump())
    assert "Иванова" not in text and "123-45-67" not in text


def test_details_url_from_deal_fields() -> None:
    spec = CONFIGS["mos_portal"].details
    assert spec is not None
    assert (
        details_url(spec, {"needId": 6322826}) == "https://zakupki.mos.ru/newapi/api/Need/Get?needId=6322826"
    )
    assert details_url(spec, {"auctionId": 10355601}).endswith("auctionId=10355601")  # type: ignore[union-attr]
    assert details_url(spec, {}) is None
