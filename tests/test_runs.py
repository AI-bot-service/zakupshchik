from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import AgentStep
from app.pipeline.runs import MANUAL_STEP, RunState, _line

T0 = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)


def _step(step: str, sec: int, **kw: object) -> AgentStep:
    return AgentStep(run_id="r", step=step, created_at=T0 + timedelta(seconds=sec), ms=kw.pop("ms", 0), **kw)


def test_collect_run_sums_platforms_and_prefilter() -> None:
    line = _line(
        "r",
        [
            _step("collect", 0, output={"platform": "mos_portal", "found": 74, "new": 2, "errors": []}),
            _step("collect", 12, output={"platform": "fabrikant", "found": 7, "errors": ["HTTP 503"]}),
            _step("prefilter", 13, output={"passed": 1, "filtered_out": 1}),
            _step("details", 14, output={"loaded": 2}, ms=500),
            _step("prefilter", 15, output={"passed": 2, "filtered_out": 0}),
        ],
    )
    assert line.kind == "collect"
    assert [p.found for p in line.platforms] == [74, 7]
    assert line.platforms[1].error == "HTTP 503"
    assert (line.passed, line.filtered_out, line.details, line.errors) == (3, 1, 2, 1)
    assert line.ms == 15_000
    assert not line.quiet


def test_analyze_run_counts_deals_calls_and_cost() -> None:
    line = _line(
        "r",
        [
            _step("agent_llm", 0, deal_id=1, model="m", tokens_in=99, tokens_out=51, cost_rub=Decimal("1.5")),
            _step("tool:search_catalog", 1, deal_id=1),
            _step("agent_llm", 2, deal_id=2, model="m", tokens_in=10, tokens_out=5, cost_rub=Decimal("0.25")),
        ],
    )
    assert line.kind == "analyze"
    assert (line.deals, line.llm_calls, line.tokens) == (2, 2, 165)
    assert line.cost_rub == Decimal("1.75")


def test_manual_run_keeps_summary() -> None:
    line = _line(
        "r",
        [
            _step(
                MANUAL_STEP,
                0,
                output={"state": "done", "by": "jury", "collect": {"found": 3}, "analyze": {"analyzed": 1}},
                ms=4200,
            )
        ],
    )
    assert line.kind == "manual"
    assert line.state is RunState.DONE
    assert line.by == "jury"
    assert line.summary == {"collect": {"found": 3}, "analyze": {"analyzed": 1}}
