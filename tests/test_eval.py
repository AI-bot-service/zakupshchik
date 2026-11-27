from decimal import Decimal

from app.eval.__main__ import Label, MatchLabel, Row, _agent, load_labels, report
from app.scoring.rules import load_rules


def test_labels_parse() -> None:
    labels = load_labels()
    assert len(labels) >= 80
    assert all(":" in k for k in labels)
    assert any(lb.ours for lb in labels.values())


def test_agent_takes_best_match() -> None:
    breakdown = {
        "analysis": {"result": {"positions": [{"match": "none"}, {"match": "analog"}]}},
        "score": {"conditions": [{"key": "catalog", "met": True}]},
    }
    assert _agent(breakdown) == ("analog", True)
    assert _agent({}) == (None, None)


def test_report_counts_errors() -> None:
    def row(
        key: str,
        ours: bool | None,
        passed: bool,
        match: MatchLabel | None = None,
        agent: str | None = None,
        met: bool | None = None,
    ) -> Row:
        label = Label(ours=ours, why="w", match=match)
        return Row(key, key, label, passed, "cut", agent, met, Decimal(1), 2.0)

    rows = [
        row("a:1", True, True, "exact", "exact", True),
        row("a:2", True, False),
        row("a:3", False, False),
        row("a:4", False, True, "none", "none", False),
        row("a:5", None, True),
    ]
    text = report(rows, load_rules())
    assert "50% (1/2)" in text  # полнота префильтра
    assert "наша, отсеяна префильтром" in text
    assert "a:4" not in text.split("**Ошибки**")[1]  # отсеял агент — не ошибка
