from datetime import UTC, date, datetime

import pytest
from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy.dialects import postgresql

from app.api.deals import _range
from app.report.excel import ALL_TIME, MONEY, _fit_row, day_period

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


def _sql(clause: object) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]


def test_day_period_covers_whole_msk_days() -> None:
    p = day_period(date(2026, 11, 26), date(2026, 11, 27))
    assert p.start == datetime(2026, 11, 25, 21, 0, tzinfo=UTC)
    assert p.end == datetime(2026, 11, 27, 21, 0, tzinfo=UTC)
    assert p.title == "найдены 26.11.2026–27.11.2026"
    sql = _sql(p.where(NOW))
    assert "deal.created_at >=" in sql and "deal.created_at <" in sql and "end_at" not in sql


def test_all_time_has_no_date_limits() -> None:
    assert "created_at" not in _sql(ALL_TIME.where(NOW))


def test_range_defaults_swaps_and_limits() -> None:
    today = date(2026, 11, 27)
    assert _range(None, None, today) == (date(2026, 11, 21), today)
    assert _range(date(2026, 11, 27), date(2026, 11, 20), today) == (date(2026, 11, 20), date(2026, 11, 27))
    assert _range(None, date(2027, 1, 1), today)[1] == today
    with pytest.raises(HTTPException):
        _range(date(2025, 1, 1), today, today)


def test_money_format_uses_locale_separator() -> None:
    assert MONEY == "#,##0.00"


def test_row_height_grows_with_wrapped_text() -> None:
    ws = Workbook().active
    assert ws is not None
    ws.append(["коротко", "\n".join(["строка"] * 5)])
    _fit_row(ws, 1, (10, 10))
    assert ws.row_dimensions[1].height == 5 * 15 + 3
