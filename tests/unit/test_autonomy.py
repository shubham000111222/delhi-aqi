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


def test_request_captures_immutable_value_snapshot_and_audit():
    store = Store(":memory:")
    approvals = Approvals(store=store)
    numbers = {"weight": 0.2, "nested": {"limit": 3}}
    request = approvals.request("allocator", "rebalance", numbers)
    numbers["nested"]["limit"] = 99
    request.numbers["weight"] = 0.9
    saved = store.approval(request.id)
    event = next(e for e in store.events() if e["kind"] == "approval_requested")
    assert saved["numbers"] == {"weight": 0.2, "nested": {"limit": 3}}
    assert event["payload"]["numbers"] == saved["numbers"]
    assert event["payload"]["authentication"] == "not_verified"
    store.close()


@pytest.mark.parametrize("numbers", [{"weight": float("nan")}, {"weight": float("inf")}, {1: 2}, {"x": "x" * 17000}])
def test_invalid_approval_payloads_are_refused_and_audited(numbers):
    store = Store(":memory:")
    with pytest.raises(ValueError):
        Approvals(store=store).request("allocator", "rebalance", numbers)
    assert store.count_events("approval_request_refused") == 1
    assert store.approvals() == []
    store.close()


def test_approval_without_store_fails_instead_of_returning_ephemeral_request():
    with pytest.raises(RuntimeError, match="durable"):
        Approvals().request("allocator", "rebalance", {"weight": 0.2})


def test_refused_and_unverified_decisions_have_distinct_audit_events():
    store = Store(":memory:")
    approvals = Approvals(store=store)
    req = approvals.request("allocator", "rebalance", {"weight": 0.2})
    with pytest.raises(ForbiddenAction):
        approvals.approve(req.id, by=" allocator ")
    with pytest.raises(KeyError):
        approvals.approve("missing", by="operator")
    approved = approvals.approve(req.id, by=" operator ")
    with pytest.raises(ValueError):
        approvals.reject(req.id, by="operator")
    assert approved.decided_by == "operator"
    assert store.count_events("approval_decision_refused") == 3
    event = next(e for e in store.events() if e["kind"] == "approval_approved")
    assert event["payload"]["authentication"] == "not_verified"
    assert event["payload"]["requester"] == "allocator"
    store.close()


def test_future_created_request_cannot_be_decided_when_clock_moves_backwards():
    now = datetime(2024, 1, 1, tzinfo=UTC)
    clock = SimClock(now)
    store = Store(":memory:")
    approvals = Approvals(store=store, clock=clock)
    req = approvals.request("allocator", "rebalance", {"weight": 0.2})
    clock.advance(-1)
    with pytest.raises(ValueError, match="changed or expired"):
        approvals.approve(req.id, by="operator")
    assert store.approval(req.id)["state"] == "pending"
    assert store.count_events("approval_decision_refused") == 1
    store.close()


def test_approval_state_and_audit_roll_back_together_on_event_failure():
    import sqlite3

    store = Store(":memory:")
    approvals = Approvals(store=store)
    req = approvals.request("allocator", "rebalance", {"weight": 0.2})
    store._exec("CREATE TRIGGER fail_approval_audit BEFORE INSERT ON events "
                "WHEN NEW.kind='approval_approved' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failure"):
        approvals.approve(req.id, by="operator")
    assert store.approval(req.id)["state"] == "pending"
    assert store.count_events("approval_approved") == 0
    store.close()


def test_request_and_audit_roll_back_together_on_event_failure():
    import sqlite3

    store = Store(":memory:")
    store._exec("CREATE TRIGGER fail_request_audit BEFORE INSERT ON events "
                "WHEN NEW.kind='approval_requested' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failure"):
        Approvals(store=store).request("allocator", "rebalance", {"weight": 0.2})
    assert store.approvals() == []
    store.close()


def test_ledger_and_audit_roll_back_together_on_event_failure():
    import sqlite3

    store = Store(":memory:")
    store._exec("CREATE TRIGGER fail_autonomy_audit BEFORE INSERT ON events "
                "WHEN NEW.kind='autonomy' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failure"):
        AutonomyLedger(store=store).record("heal", "reconnect", AutonomyLevel.CORRECT)
    assert store.autonomy_actions() == []
    assert store.count_events("autonomy") == 0
    store.close()


def test_decision_race_across_independent_database_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "approval.db"
    one, two = Store(path), Store(path)
    request = Approvals(store=one).request("allocator", "rebalance", {"weight": 0.2})
    barrier = Barrier(2)

    def decide(args):
        store, who = args
        barrier.wait()
        try:
            return Approvals(store=store).approve(request.id, by=who).decided_by
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(decide, [(one, "one"), (two, "two")]))
    assert len([outcome for outcome in outcomes if outcome]) == 1
    assert one.count_events("approval_approved") == 1
    assert one.count_events("approval_decision_refused") == 1
    one.close()
    two.close()
    reopened = Store(path)
    assert reopened.approval(request.id)["decided_by"] in outcomes
    assert reopened.count_events("approval_approved") == 1
    reopened.close()


def test_store_rejects_direct_self_approval():
    store = Store(":memory:")
    approvals = Approvals(store=store)
    req = approvals.request("allocator", "rebalance", {"weight": 0.2})
    assert not store.transition_approval(req.id, state="approved", now=approvals.now(), by=" allocator ")
    assert store.approval(req.id)["state"] == "pending"
    store.close()
