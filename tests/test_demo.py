from datetime import UTC, datetime, timedelta

from app.api.journal import _release_at


def test_release_empty_is_ten_seconds() -> None:
    now = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)
    assert _release_at("", now) == now + timedelta(seconds=10)


def test_release_is_moscow_time_today() -> None:
    now = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)  # 10:00 МСК
    assert _release_at("10:30", now) == datetime(2026, 11, 28, 7, 30, tzinfo=UTC)


def test_release_past_time_moves_to_tomorrow() -> None:
    now = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)
    assert _release_at("09:00", now) == datetime(2026, 11, 29, 6, 0, tzinfo=UTC)
