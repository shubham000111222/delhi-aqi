from datetime import UTC, datetime, timedelta

import pytest

from tradebot.autonomy import (
    FORBIDDEN_ACTIONS,
    ApprovalRequired,
    Approvals,
    AutonomyLedger,
    AutonomyLevel,
    ForbiddenAction,
)
from tradebot.core.clock import SimClock
from tradebot.core.store import Store


def test_ledger_persists_actions():
    store = Store(":memory:")
    ledger = AutonomyLedger(store=store)
    ledger.record("heal", "reconnect", AutonomyLevel.CORRECT, "feed")
    assert ledger.recent()[0]["action"] == "reconnect"
    assert ledger.summary()["by_actor"] == {"heal": 1}
    store.close()


@pytest.mark.parametrize("action", FORBIDDEN_ACTIONS)
def test_ledger_refuses_forbidden_actions_even_with_claimed_approval(action):
    store = Store(":memory:")
    with pytest.raises(ForbiddenAction):
        AutonomyLedger(store=store).record("model", action, 3, approved_by="operator")
    assert store.count_events("autonomy_refused") == 1
    store.close()


def test_caller_cannot_grant_allocation_authority_by_naming_an_approver():
    with pytest.raises(ApprovalRequired):
        AutonomyLedger().record("model", "rebalance", 4, approved_by="operator")


def test_approval_expires_and_cannot_be_self_approved():
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    store = Store(":memory:")
    approvals = Approvals(store=store, clock=clock)
    req = approvals.request("allocator", "rebalance", {"weight": 0.2}, ttl=timedelta(minutes=1))
    with pytest.raises(ForbiddenAction):
        approvals.approve(req.id, by="allocator")
    clock.advance(timedelta(minutes=1))
    with pytest.raises(ValueError, match="expired"):
        approvals.approve(req.id, by="operator")
    assert store.approval(req.id)["state"] == "expired"
    store.close()


def test_approved_request_is_not_rewritten_as_expired():
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    store = Store(":memory:")
    approvals = Approvals(store=store, clock=clock)
    req = approvals.request("allocator", "rebalance", {"weight": 0.2}, ttl=timedelta(minutes=1))
    approvals.approve(req.id, by="operator")
    clock.advance(timedelta(minutes=2))
    with pytest.raises(ValueError, match="already approved"):
        approvals.approve(req.id, by="operator")
    assert store.approval(req.id)["state"] == "approved"
    store.close()


def test_decisions_are_atomic_across_racing_workers():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = Store(":memory:")
    approvals = Approvals(store=store)
    req = approvals.request("allocator", "rebalance", {"weight": 0.2})
    barrier = Barrier(2)

    def decide(by):
        barrier.wait()
        try:
            return approvals.approve(req.id, by=by).decided_by
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(decide, ["operator1", "operator2"]))
    assert len([value for value in outcomes if value is not None]) == 1
    assert store.approval(req.id)["decided_by"] in outcomes
    store.close()


def test_expiry_checks_all_requests_not_only_first_page():
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    store = Store(":memory:")
    approvals = Approvals(store=store, clock=clock)
    for _ in range(55):
        approvals.request("allocator", "rebalance", {"weight": 0.2}, ttl=timedelta(seconds=1))
    clock.advance(1)
    assert approvals.expire() == 55
    assert approvals.pending() == []
    store.close()


def test_subsecond_deadline_is_compared_chronologically():
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    store = Store(":memory:")
    approvals = Approvals(store=store, clock=clock)
    request = approvals.request("allocator", "rebalance", {"weight": 0.2}, ttl=timedelta(microseconds=1))
    assert approvals.approve(request.id, by="operator").state == "approved"
    store.close()
