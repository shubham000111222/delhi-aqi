"""Only a trusted, current, single-use signature can lower the live cap."""
import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tradebot.config import Mode
from tradebot.core.clock import SimClock
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.risk.live_guards import RampController
from tradebot.signed_allocation import SignedAllocation
from tradebot.signed_recovery import canonical_bytes, key_id

NOW = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def setup(fast_cfg, tmp_path):
    cfg = replace(fast_cfg, settings=fast_cfg.settings.model_copy(update={"mode": Mode.LIVE}))
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = key_id(public)
    keyfile = cfg.settings.config_dir.resolve().parent / "operator-keys.json"
    keyfile.write_text(json.dumps({"version": 1, "keys": {fingerprint: base64.b64encode(public).decode()}}))
    store = Store(tmp_path / "allocation.db")
    service = SignedAllocation(cfg, store, clock=SimClock(NOW))
    yield service, store, private, fingerprint, keyfile
    store.close()


def sign(key, request):
    return base64.b64encode(key.sign(canonical_bytes(request))).decode()


def test_signed_reduction_changes_actual_live_sizing_and_cannot_replay(setup):
    service, store, key, fingerprint, _ = setup
    req = service.request(fingerprint, "reduce risk", "0.04")
    service.apply(req, sign(key, req))
    assert RampController(service.config, store).multiplier() == D("0.04")
    assert store.count_events("signed_allocation_reduced") == 1
    assert store.autonomy_actions()[-1]["approved_by"] == f"key:{fingerprint}"
    with pytest.raises(PermissionError, match="consumed"):
        service.apply(req, sign(key, req))
    with pytest.raises(PermissionError, match="only be reduced"):
        service.request(fingerprint, "raise", "0.05")


@pytest.mark.parametrize("cap", ["0", "-1", "NaN", "Infinity", "0.11", "garbage"])
def test_invalid_cap_is_rejected(setup, cap):
    service, _, _, fingerprint, _ = setup
    with pytest.raises(ValueError, match="cap"):
        service.request(fingerprint, "invalid", cap)


def test_wrong_signature_stale_and_revoked(setup):
    service, store, key, fingerprint, keyfile = setup
    req = service.request(fingerprint, "reduce", "0.05")
    with pytest.raises(PermissionError, match="signature"):
        service.apply(req, sign(Ed25519PrivateKey.generate(), req))
    signed = sign(key, req)
    req["new_cap"] = "0.02"
    with pytest.raises(PermissionError, match="signature"):
        service.apply(req, signed)
    req["new_cap"] = "0.05"
    store.kv_set("operator_allocation_cap", {"cap": "0.06"})
    with pytest.raises(PermissionError, match="stale"):
        service.apply(req, signed)
    keyfile.write_text('{"version":1,"keys":{}}')
    with pytest.raises(PermissionError, match="not trusted"):
        service.apply(req, signed)


def test_expiry_and_rollback(setup):
    service, store, key, fingerprint, _ = setup
    req = service.request(fingerprint, "reduce", "0.05", ttl=timedelta(seconds=1))
    service.clock.advance(1)
    with pytest.raises(PermissionError, match="expired"):
        service.apply(req, sign(key, req))
    service.clock = SimClock(NOW)
    store._exec("CREATE TRIGGER fail_cap BEFORE INSERT ON events WHEN NEW.kind='signed_allocation_reduced' "
                "BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        service.apply(req, sign(key, req))
    assert store.kv_get("operator_allocation_cap") is None
    assert not store._query("SELECT * FROM signed_action_consumptions")
    assert not store.autonomy_actions()


def test_concurrent_consumption(setup):
    service, store, key, fingerprint, _ = setup
    req = service.request(fingerprint, "reduce", "0.05")
    signature = sign(key, req)

    def consume(_):
        db = Store(store.path)
        try:
            SignedAllocation(service.config, db, clock=SimClock(NOW)).apply(req, signature)
            return True
        except PermissionError:
            return False
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]
    assert store.count_events("signed_allocation_reduced") == 1


@pytest.mark.parametrize("raw", [None, {}, {"cap": "NaN"}, {"cap": "0.11"}, {"cap": 0.05}])
def test_corrupt_operator_cap_blocks_sizing(setup, raw):
    service, store, _, _, _ = setup
    store.kv_set("operator_allocation_cap", raw)
    with pytest.raises(ValueError, match="cap"):
        RampController(service.config, store).multiplier()


def test_wrong_database_and_mode_are_rejected(setup, tmp_path):
    service, _, key, fingerprint, _ = setup
    req = service.request(fingerprint, "reduce", "0.05")
    signature = sign(key, req)
    other = Store(tmp_path / "other.db")
    try:
        with pytest.raises(PermissionError, match="database mismatch"):
            SignedAllocation(service.config, other, clock=SimClock(NOW)).apply(req, signature)
        paper = replace(service.config, settings=service.config.settings.model_copy(update={"mode": Mode.PAPER}))
        with pytest.raises(PermissionError, match="mode mismatch"):
            SignedAllocation(paper, other, clock=SimClock(NOW)).apply(req, signature)
    finally:
        other.close()


def test_expiry_is_checked_after_lock(setup, monkeypatch):
    from contextlib import contextmanager

    service, store, key, fingerprint, _ = setup
    req = service.request(fingerprint, "reduce", "0.05", ttl=timedelta(seconds=1))
    original = store.transaction

    @contextmanager
    def delayed():
        with original() as conn:
            service.clock.advance(2)
            yield conn

    monkeypatch.setattr(store, "transaction", delayed)
    with pytest.raises(PermissionError, match="expired"):
        service.apply(req, sign(key, req))
    assert store.kv_get("operator_allocation_cap") is None
