from datetime import UTC, datetime, timedelta

from app.db.models import Deal, DealStatus
from app.web.view import Verdict, time_left, urgent, verdict

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


def test_time_left_in_plain_words() -> None:
    assert time_left(None, NOW) == "срок не указан"
    assert time_left(NOW - timedelta(minutes=1), NOW) == "приём закрыт"
    assert time_left(NOW + timedelta(minutes=40), NOW) == "осталось 40 мин"
    assert time_left(NOW + timedelta(hours=4, minutes=20), NOW) == "осталось 4 ч 20 мин"
    assert time_left(NOW + timedelta(days=3, hours=5), NOW) == "осталось 3 дн. 5 ч"


def test_urgent_only_within_a_day() -> None:
    assert urgent(NOW + timedelta(hours=23), NOW)
    assert not urgent(NOW + timedelta(hours=25), NOW)
    assert not urgent(NOW - timedelta(hours=1), NOW)


def test_verdict_follows_conditions() -> None:
    def deal(n: int | None, status: DealStatus = DealStatus.ANALYZED, catalog: bool = True) -> Deal:
        conditions = [{"key": "catalog", "met": catalog, "title": "ПО из каталога", "reason": "-"}]
        return Deal(conditions_met=n, status=status, score_breakdown={"score": {"conditions": conditions}})

    assert verdict(deal(5)) is Verdict.TAKE
    assert verdict(deal(4, DealStatus.HOT)) is Verdict.TAKE
    assert verdict(deal(4)) is Verdict.DECIDE
    assert verdict(deal(3)) is Verdict.SKIP
    assert verdict(deal(None)) is Verdict.WAIT
    assert verdict(deal(None, DealStatus.FILTERED_OUT)) is Verdict.SKIP
    # Позиции нет в каталоге — решает менеджер, автоматически не отсеиваем
    assert verdict(deal(4, catalog=False)) is Verdict.DECIDE
    assert verdict(deal(3, catalog=False)) is Verdict.DECIDE
    assert verdict(deal(2, catalog=False)) is Verdict.SKIP
