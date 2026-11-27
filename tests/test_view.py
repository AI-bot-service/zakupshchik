from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.deals import apply_status
from app.db.models import Actor, Channel, Deal, DealStatus, DealStatusLog, Proposal, ProposalStatus
from app.pipeline.status_log import Who
from app.report.stamps import Stamp, Verdict, log_line, stamp, verdict
from app.web.view import can_skip, next_actions, time_left, track, urgent

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

    assert verdict(deal(6)) is Verdict.TAKE
    assert verdict(deal(5, DealStatus.HOT)) is Verdict.TAKE
    assert verdict(deal(5)) is Verdict.DECIDE
    assert verdict(deal(4)) is Verdict.SKIP
    assert verdict(deal(None)) is Verdict.WAIT
    assert verdict(deal(None, DealStatus.FILTERED_OUT)) is Verdict.SKIP
    # Позиции нет в каталоге — решает менеджер, автоматически не отсеиваем
    assert verdict(deal(5, catalog=False)) is Verdict.DECIDE
    assert verdict(deal(4, catalog=False)) is Verdict.DECIDE
    assert verdict(deal(3, catalog=False)) is Verdict.SKIP


def test_stamp_shows_manager_decision_with_date() -> None:
    at = datetime(2026, 11, 27, 11, 30, tzinfo=UTC)  # 14:30 МСК
    open_end = NOW + timedelta(days=2)

    def st(
        status: DealStatus, end: datetime = open_end, has_kp: bool = False, n: int | None = 5
    ) -> tuple[Stamp, str]:
        d = Deal(status=status, end_at=end, conditions_met=n, score_breakdown={})
        return stamp(d, verdict(d), has_kp, at, NOW)

    assert st(DealStatus.SUBMITTED) == (Stamp.SENT, "27.11 14:30")
    assert st(DealStatus.TAKEN) == (Stamp.WORK, "с 27.11 14:30 · КП собирается")
    assert st(DealStatus.TAKEN, has_kp=True) == (Stamp.WORK, "с 27.11 14:30 · КП готово")
    assert st(DealStatus.TAKEN, end=NOW - timedelta(hours=1)) == (Stamp.LATE, "КП не отправлено")
    assert st(DealStatus.SKIPPED) == (Stamp.DECLINED, "27.11 14:30")
    assert st(DealStatus.ANALYZED) == (Stamp.DECIDE, "5 из 6 условий")
    assert st(DealStatus.ANALYZED, end=NOW - timedelta(hours=1))[0] is Stamp.CLOSED
    assert st(DealStatus.EXPIRED)[0] is Stamp.CLOSED
    assert st(DealStatus.FILTERED_OUT, n=None)[0] is Stamp.FILTERED
    assert st(DealStatus.NEW, n=None) == (Stamp.WAIT, "")


WHO = Who(Actor.MANAGER, "manager", Channel.EXTENSION, "рабочий ноутбук")


class FakeSession:
    def __init__(self, proposal: Proposal | None) -> None:
        self.proposal = proposal
        self.added: list[Any] = []

    def scalar(self, _q: Any) -> Proposal | None:
        return self.proposal

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        pass


def test_submitted_without_kp_is_refused() -> None:
    deal = Deal(id=1, status=DealStatus.TAKEN, score_breakdown={})
    session = FakeSession(None)
    with pytest.raises(HTTPException) as exc:
        apply_status(session, deal, DealStatus.SUBMITTED, "", WHO)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert deal.status is DealStatus.TAKEN and not session.added


def test_decisions_go_to_deal_journal_once() -> None:
    proposal = Proposal(id=5, deal_id=1, version=2, items={}, status=ProposalStatus.DRAFT)
    deal = Deal(id=1, status=DealStatus.ANALYZED, conditions_met=5, rules_version="abc", score_breakdown={})
    session = FakeSession(proposal)
    apply_status(session, deal, DealStatus.TAKEN, "", WHO)  # type: ignore[arg-type]
    apply_status(session, deal, DealStatus.TAKEN, "", WHO)  # type: ignore[arg-type]  # повтор — без записи
    apply_status(session, deal, DealStatus.SUBMITTED, "", WHO)  # type: ignore[arg-type]
    apply_status(session, deal, DealStatus.SKIPPED, "дорого", WHO)  # type: ignore[arg-type]
    apply_status(session, deal, DealStatus.SKIPPED, "нет партнёрства", WHO)  # type: ignore[arg-type]
    log = [e for e in session.added if isinstance(e, DealStatusLog)]
    assert [(e.from_status, e.to_status) for e in log] == [
        (DealStatus.ANALYZED, DealStatus.TAKEN),
        (DealStatus.TAKEN, DealStatus.SUBMITTED),
        (DealStatus.SUBMITTED, DealStatus.SKIPPED),
        (DealStatus.SKIPPED, DealStatus.SKIPPED),
    ]
    assert proposal.status is ProposalStatus.SENT
    sent = log_line(log[1])
    assert sent.move == "в работе → КП отправлено"
    assert sent.who == "manager, плагин «рабочий ноутбук»"
    assert sent.facts == "5 из 6, КП v2, правила abc"
    assert log[3].reason == "нет партнёрства"


def test_back_to_work_unmarks_sent_kp() -> None:
    proposal = Proposal(id=5, deal_id=1, version=2, items={"source": "manager"}, status=ProposalStatus.SENT)
    deal = Deal(id=1, status=DealStatus.SUBMITTED, conditions_met=5, rules_version="abc", score_breakdown={})
    apply_status(FakeSession(proposal), deal, DealStatus.TAKEN, "", WHO)  # type: ignore[arg-type]
    assert deal.status is DealStatus.TAKEN
    assert proposal.status is ProposalStatus.DRAFT  # файл КП остаётся, отметка «отправлено» снята


def _log(i: int, before: DealStatus | None, after: DealStatus) -> DealStatusLog:
    return DealStatusLog(
        id=i,
        at=NOW - timedelta(hours=10 - i),
        from_status=before,
        to_status=after,
        actor=Actor.MANAGER,
        actor_name="m",
        channel=Channel.STAND,
        details={},
    )


def _deal(status: DealStatus, n: int | None = 5, end: timedelta = timedelta(days=1)) -> Deal:
    return Deal(id=1, status=status, conditions_met=n, created_at=NOW - timedelta(hours=12), end_at=NOW + end)


def test_track_follows_journal() -> None:
    log = [_log(1, DealStatus.NEW, DealStatus.ANALYZED), _log(2, DealStatus.ANALYZED, DealStatus.TAKEN)]
    steps = track(_deal(DealStatus.TAKEN), log, NOW)
    assert [(s.name, s.state) for s in steps] == [
        ("Найдена", "done"),
        ("Разобрана", "done"),
        ("В работе", "now"),
        ("КП отправлено", "next"),
    ]
    late = track(_deal(DealStatus.TAKEN, end=-timedelta(hours=1)), log, NOW)
    assert [(s.name, s.state) for s in late][-1] == ("Срок вышел", "stop")
    skipped = track(_deal(DealStatus.SKIPPED), [*log, _log(3, DealStatus.TAKEN, DealStatus.SKIPPED)], NOW)
    assert [s.name for s in skipped] == ["Найдена", "Разобрана", "В работе", "Отказ"]
    assert [s.name for s in track(_deal(DealStatus.FILTERED_OUT, None), [], NOW)] == ["Найдена", "Отсеяна"]
    sent = track(_deal(DealStatus.SUBMITTED), [*log, _log(3, DealStatus.TAKEN, DealStatus.SUBMITTED)], NOW)
    assert all(s.state == "done" for s in sent) and len(sent) == 4


def test_actions_match_status() -> None:
    def to(d: Deal, kp: bool = False) -> list[DealStatus]:
        return [a.to for a in next_actions(d, kp, NOW)]

    assert to(_deal(DealStatus.ANALYZED)) == [DealStatus.TAKEN]
    assert to(_deal(DealStatus.TAKEN)) == [DealStatus.SUBMITTED]
    assert to(_deal(DealStatus.SUBMITTED)) == [DealStatus.TAKEN]
    assert to(_deal(DealStatus.SKIPPED)) == [DealStatus.TAKEN]
    assert to(_deal(DealStatus.ANALYZED, end=-timedelta(hours=1))) == []
    assert to(_deal(DealStatus.FILTERED_OUT, None)) == [] and not can_skip(
        _deal(DealStatus.FILTERED_OUT, None), NOW
    )
    assert can_skip(_deal(DealStatus.TAKEN, end=-timedelta(hours=1)), NOW)
    assert not can_skip(_deal(DealStatus.SUBMITTED), NOW)


def test_take_needs_analysis() -> None:
    deal = Deal(id=1, status=DealStatus.NEW, conditions_met=None, score_breakdown={})
    with pytest.raises(HTTPException) as exc:
        apply_status(FakeSession(None), deal, DealStatus.TAKEN, "", WHO)  # type: ignore[arg-type]
    assert exc.value.status_code == 422
