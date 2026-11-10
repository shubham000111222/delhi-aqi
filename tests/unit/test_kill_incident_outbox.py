"""Kill escalations keep a replayable audit hold across failures and restarts."""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tradebot.config import Mode
from tradebot.core.clock import SimClock
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch
from tradebot.risk.live_guards import RampController

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def fail_incident(store):
    store._exec("CREATE TRIGGER fail_incident BEFORE INSERT ON incidents "
                "BEGIN SELECT RAISE(ABORT, 'incident write failed'); END")


def test_escalation_survives_audit_failure_and_recovers_after_restart(tmp_path, cfg):
    path = tmp_path / "state.db"
    store = Store(path)
    clock = SimClock(NOW)
    switch = KillSwitch(store, clock=clock, kill_file=None)
    fail_incident(store)
    assert switch.trigger(KillLevel.L2, trigger="test", reason="emergency")
    assert switch.level is KillLevel.L2
    assert store.has_pending_kill_incident()
    assert not store.incidents()
    with pytest.raises(PermissionError, match="incident tracking"):
        switch.reset(reason="review")
    config = replace(cfg, settings=cfg.settings.model_copy(update={"mode": Mode.LIVE}))
    with pytest.raises(ValueError, match="unrecorded kill incident"):
        RampController(config, store).multiplier()
    store._exec("DROP TRIGGER fail_incident")
    store.close()
    recovered = Store(path)
    reopened = KillSwitch(recovered, clock=clock, kill_file=None)
    assert reopened.level is KillLevel.L2
    assert not recovered.has_pending_kill_incident()
    assert len(recovered.incidents()) == 1
    assert recovered.count_events("ramp_incident") == 1
    assert RampController(config, recovered).load().critical_incidents == 1
    assert recovered.recover_pending_kill_incidents() == 0
    recovered.close()


def test_recovery_failure_keeps_the_hold(cfg):
    store = Store(":memory:")
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None)
    fail_incident(store)
    switch.trigger(KillLevel.L1, trigger="stale_feeds", reason="pause")
    reopened = KillSwitch(store, clock=SimClock(NOW), kill_file=None)
    assert store.has_pending_kill_incident()
    assert not reopened.auto_resume(reason="recovered", expected_state=reopened.state)
    assert reopened.level is KillLevel.L1
    with pytest.raises(PermissionError, match="incident tracking"):
        reopened.reset(reason="manual")
    store.close()


def test_outbox_mismatch_does_not_clear_pending():
    store = Store(":memory:")
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None)
    fail_incident(store)
    switch.trigger(KillLevel.L2, trigger="test", reason="emergency")
    store._exec("DROP TRIGGER fail_incident")
    with pytest.raises(ValueError, match="does not match"):
        store.add_incident("L2", "wrong", "emergency", ts=NOW,
                           kill_generation=switch.state["generation"])
    assert store.has_pending_kill_incident()
    assert not store.incidents()
    assert store.recover_pending_kill_incidents() == 1
    assert not store.has_pending_kill_incident()
    assert len(store.incidents()) == 1
    store.close()


def test_outbox_failure_preserves_kill_and_latches_tracking_fault(cfg):
    store = Store(":memory:")
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None)
    store._exec("CREATE TRIGGER fail_outbox BEFORE INSERT ON kv "
                "WHEN NEW.key GLOB 'kill_incident_pending:*' "
                "BEGIN SELECT RAISE(ABORT, 'outbox failed'); END")
    assert switch.trigger(KillLevel.L2, trigger="test", reason="emergency")
    assert switch.level is KillLevel.L2
    assert switch.state["incident_tracking_failed"] is True
    assert not store.has_pending_kill_incident()
    with pytest.raises(PermissionError, match="incident tracking"):
        switch.reset(reason="review")
    config = replace(cfg, settings=cfg.settings.model_copy(update={"mode": Mode.LIVE}))
    with pytest.raises(ValueError, match="unrecorded kill incident"):
        RampController(config, store).multiplier()
    store.close()


def test_pending_incidents_for_multiple_escalations_recover_once():
    store = Store(":memory:")
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None)
    fail_incident(store)
    assert switch.trigger(KillLevel.L1, trigger="feed", reason="pause")
    assert switch.trigger(KillLevel.L2, trigger="manual", reason="flatten")
    assert len(store._query("SELECT key FROM kv WHERE key GLOB 'kill_incident_pending:*'")) == 2
    store._exec("DROP TRIGGER fail_incident")
    assert store.recover_pending_kill_incidents() == 2
    assert len(store.incidents()) == 2
    assert store.recover_pending_kill_incidents() == 0
    assert store.kill_level() == "L2"
    store.close()


def test_stale_outbox_completion_cannot_clear_another_transition():
    store = Store(":memory:")
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None)
    fail_incident(store)
    switch.trigger(KillLevel.L1, trigger="feed", reason="pause")
    first = switch.state["generation"]
    switch.trigger(KillLevel.L2, trigger="kill", reason="flatten")
    second = switch.state["generation"]
    store._exec("DROP TRIGGER fail_incident")
    store.add_incident("L1", "feed", "pause", ts=NOW, kill_generation=first)
    assert store.has_pending_kill_incident()
    with pytest.raises(ValueError, match="no longer pending"):
        store.add_incident("L1", "feed", "pause", ts=NOW, kill_generation=first)
    assert store.kv_get(f"kill_incident_pending:{second}") is not None
    assert store.recover_pending_kill_incidents() == 1
    assert len(store.incidents()) == 2
    store.close()


def test_entry_boundary_fails_closed_if_pending_exists_even_at_l0():
    store = Store(":memory:")
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None)
    store.kv_set("kill_incident_pending:999", {"damaged": True})
    assert switch.level is KillLevel.L0
    assert not switch.can_open_entries()
    store.kv_delete("kill_incident_pending:999")
    state = switch.state
    state["incident_tracking_failed"] = True
    store.kv_set("kill_state", state)
    assert not switch.can_open_entries()
    store.close()
