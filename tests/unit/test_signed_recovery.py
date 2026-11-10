"""Offline approval consumes a signature once and never resumes trading."""
import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tradebot.config import Mode
from tradebot.core.clock import SimClock
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch
from tradebot.risk.live_guards import RampController
from tradebot.signed_recovery import SignedFaultRecovery, canonical_bytes, key_id

NOW = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def setup(fast_cfg, tmp_path):
    private = Ed25519PrivateKey.generate()
    pub = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = key_id(pub)
    keyfile = fast_cfg.settings.config_dir.resolve().parent / "operator-keys.json"
    keyfile.write_text(json.dumps({"version": 1, "keys": {fingerprint: base64.b64encode(pub).decode()}}))
    store = Store(tmp_path / "recovery.db")
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None, mode=Mode.PAPER)
    store._exec("CREATE TRIGGER fail_outbox BEFORE INSERT ON kv WHEN NEW.key GLOB 'kill_incident_pending:*' "
                "BEGIN SELECT RAISE(ABORT, 'outbox failed'); END")
    assert switch.trigger(KillLevel.L2, trigger="test", reason="emergency")
    store._exec("DROP TRIGGER fail_outbox")
    service = SignedFaultRecovery(fast_cfg, store, clock=SimClock(NOW))
    yield service, store, switch, private, fingerprint, keyfile
    store.close()


def sign(private, req):
    return base64.b64encode(private.sign(canonical_bytes(req))).decode()


def test_valid_signature_recovers_fault_once_without_resuming(setup):
    service, store, switch, private, fingerprint, _ = setup
    request = service.request(fingerprint, "reviewed missing incident")
    signature = sign(private, request)
    old_generation = switch.state["generation"]
    service.apply(request, signature)
    assert switch.level is KillLevel.L2
    assert switch.state["incident_tracking_failed"] is False
    assert switch.state["generation"] != old_generation
    assert not store.incidents(), "unverified incidents must not be fabricated"
    assert store.count_events("signed_tracking_fault_ack") == 1
    assert RampController(service.config, store).load().critical_incidents == 0
    assert store.kv_get("ramp_incident_tracking_invalid") is not None
    with pytest.raises(PermissionError, match="consumed"):
        service.apply(request, signature)
    assert not store.incidents()


def test_wrong_key_tamper_and_revocation_are_refused(setup):
    service, store, switch, private, fingerprint, keyfile = setup
    request = service.request(fingerprint, "review")
    with pytest.raises(PermissionError, match="signature"):
        service.apply(request, sign(Ed25519PrivateKey.generate(), request))
    signature = sign(private, request)
    request["reason"] = "tampered"
    with pytest.raises(PermissionError, match="signature"):
        service.apply(request, signature)
    request["reason"] = "review"
    keyfile.write_text('{"version":1,"keys":{}}')
    with pytest.raises(PermissionError, match="not trusted"):
        service.apply(request, signature)
    assert switch.state["incident_tracking_failed"] is True
    assert not store.incidents()


def test_expired_and_stale_requests_are_refused(setup):
    service, store, switch, private, fingerprint, _ = setup
    request = service.request(fingerprint, "review", ttl=timedelta(minutes=1))
    signature = sign(private, request)
    service.clock.advance(60)
    with pytest.raises(PermissionError, match="expired"):
        service.apply(request, signature)
    service.clock = SimClock(NOW)
    switch.trigger(KillLevel.L3, trigger="new", reason="new event")
    with pytest.raises(PermissionError, match="stale"):
        service.apply(request, signature)
    assert switch.state["incident_tracking_failed"] is True
    assert store.count_events("signed_tracking_fault_ack") == 0


def test_transaction_rolls_back_all_changes_on_audit_failure(setup):
    service, store, switch, private, fingerprint, _ = setup
    request = service.request(fingerprint, "review")
    store._exec("CREATE TRIGGER fail_signed BEFORE INSERT ON events WHEN NEW.kind='signed_tracking_fault_ack' "
                "BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        service.apply(request, sign(private, request))
    assert switch.state["incident_tracking_failed"] is True
    assert not store.incidents()
    assert RampController(service.config, store).load().critical_incidents == 0
    assert not store._query("SELECT * FROM signed_action_consumptions")


def test_approval_is_bound_to_database(setup, tmp_path):
    service, _, _, private, fingerprint, _ = setup
    request = service.request(fingerprint, "review")
    other = Store(tmp_path / "other.db")
    try:
        other_service = SignedFaultRecovery(service.config, other, clock=SimClock(NOW))
        with pytest.raises(PermissionError, match="database mismatch"):
            other_service.apply(request, sign(private, request))
    finally:
        other.close()


def test_concurrent_consumers_cannot_apply_twice(setup):
    service, store, switch, private, fingerprint, _ = setup
    request = service.request(fingerprint, "review")
    signature = sign(private, request)

    def consume(_):
        db = Store(store.path)
        try:
            SignedFaultRecovery(service.config, db, clock=SimClock(NOW)).apply(request, signature)
            return True
        except PermissionError:
            return False
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))
    assert sorted(results) == [False, True]
    assert not store.incidents()
    assert store.kv_get("ramp_incident_tracking_invalid") is not None
    assert switch.level is KillLevel.L2


def test_marker_blocks_reset_and_entries_after_signed_ack(setup):
    service, store, switch, private, fingerprint, _ = setup
    request = service.request(fingerprint, "acknowledge unverified history")
    service.apply(request, sign(private, request))
    with pytest.raises(PermissionError, match="incident tracking"):
        switch.reset(reason="reviewed")
    assert switch.level is KillLevel.L2
    assert not switch.can_open_entries()


def test_null_tracking_marker_also_blocks_entries_and_reset(setup):
    _, store, switch, _, _, _ = setup
    store.kv_set("ramp_incident_tracking_invalid", None)
    assert store.has_invalid_ramp_tracking()
    assert not switch.can_open_entries()
    with pytest.raises(PermissionError, match="incident tracking"):
        switch.reset(reason="reviewed")


def test_expiry_is_rechecked_after_database_lock(setup, monkeypatch):
    from contextlib import contextmanager

    service, store, switch, private, fingerprint, _ = setup
    request = service.request(fingerprint, "review", ttl=timedelta(seconds=1))
    original = store.transaction

    @contextmanager
    def delayed_transaction():
        with original() as conn:
            service.clock.advance(2)
            yield conn

    monkeypatch.setattr(store, "transaction", delayed_transaction)
    with pytest.raises(PermissionError, match="expired while waiting"):
        service.apply(request, sign(private, request))
    assert switch.state["incident_tracking_failed"] is True
    assert store.count_events("signed_tracking_fault_ack") == 0


def test_signed_request_contains_reviewable_state_and_rejects_state_tampering(setup):
    service, store, switch, private, fingerprint, _ = setup
    request = service.request(fingerprint, "review")
    assert request["state"] == switch.state
    assert request["state"]["level"] == "L2"
    assert request["state"]["reason"] == "emergency"
    signature = sign(private, request)
    request["state"]["reason"] = "changed"
    with pytest.raises(ValueError, match="state hash mismatch"):
        service.apply(request, signature)
    assert switch.state["incident_tracking_failed"] is True
    assert store.count_events("signed_tracking_fault_ack") == 0
