import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tradebot.core.clock import SimClock
from tradebot.core.store import Store
from tradebot.signed_recovery import canonical_bytes, key_id
from tradebot.signed_strategy import SignedStrategyReduction

NOW = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def setup(fast_cfg, tmp_path):
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = key_id(raw)
    bundle = fast_cfg.settings.config_dir.resolve().parent / "operator-keys.json"
    bundle.write_text(json.dumps({"version": 1, "keys": {fingerprint: base64.b64encode(raw).decode()}}))
    store = Store(tmp_path / "strategy.db")
    service = SignedStrategyReduction(fast_cfg, store, clock=SimClock(NOW))
    name = next(iter(fast_cfg.enabled_strategies))
    yield service, store, private, fingerprint, bundle, name
    store.close()


def sign(key, req):
    return base64.b64encode(key.sign(canonical_bytes(req))).decode()


def test_signed_reduction_and_single_use(setup):
    service, store, key, fingerprint, _, name = setup
    req = service.request(fingerprint, "reduce risk", name, "0.4")
    signature = sign(key, req)
    service.apply(req, signature)
    assert store.strategy_size_multiplier(name) == Decimal("0.4")
    assert store.count_events("signed_strategy_size_reduced") == 1
    assert store.autonomy_actions()[-1]["approved_by"] == f"key:{fingerprint}"
    with pytest.raises(PermissionError, match="consumed"):
        service.apply(req, signature)
    with pytest.raises(PermissionError, match="only be reduced"):
        service.request(fingerprint, "raise", name, "0.5")


@pytest.mark.parametrize("value", ["0", "-1", "1.1", "NaN", "Infinity", "bad", "1"])
def test_invalid_or_non_reducing_request(setup, value):
    service, _, _, fingerprint, _, name = setup
    with pytest.raises((ValueError, PermissionError)):
        service.request(fingerprint, "reason", name, value)


def test_tamper_staleness_revocation_and_wrong_strategy(setup):
    service, store, key, fingerprint, bundle, name = setup
    with pytest.raises(ValueError, match="strategy"):
        service.request(fingerprint, "reason", "unknown", "0.5")
    req = service.request(fingerprint, "reason", name, "0.5")
    signature = sign(key, req)
    req["new_multiplier"] = "0.2"
    with pytest.raises(PermissionError, match="signature"):
        service.apply(req, signature)
    req["new_multiplier"] = "0.5"
    store.reduce_strategy_size(name, Decimal("0.7"), now=NOW, reason="other")
    with pytest.raises(PermissionError, match="stale"):
        service.apply(req, signature)
    bundle.write_text('{"version":1,"keys":{}}')
    with pytest.raises(PermissionError, match="not trusted"):
        service.apply(req, signature)


def test_expiry_and_transaction_rollback(setup):
    service, store, key, fingerprint, _, name = setup
    req = service.request(fingerprint, "reason", name, "0.5", ttl=timedelta(seconds=1))
    service.clock.advance(1)
    with pytest.raises(PermissionError, match="expired"):
        service.apply(req, sign(key, req))
    service.clock = SimClock(NOW)
    store._exec("CREATE TRIGGER fail_signed_strategy BEFORE INSERT ON events "
                "WHEN NEW.kind='signed_strategy_size_reduced' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.apply(req, sign(key, req))
    assert store.strategy_size_multiplier(name) == Decimal("1")
    assert not store._query("SELECT * FROM signed_action_consumptions")
    assert not store.autonomy_actions()


def test_concurrent_consumption_is_once(setup):
    service, store, key, fingerprint, _, name = setup
    req = service.request(fingerprint, "reason", name, "0.5")
    signature = sign(key, req)

    def consume(_):
        db = Store(store.path)
        try:
            SignedStrategyReduction(service.config, db, clock=SimClock(NOW)).apply(req, signature)
            return True
        except PermissionError:
            return False
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]
    assert store.count_events("signed_strategy_size_reduced") == 1


def test_wrong_database_and_mode_refused(setup, tmp_path):
    from dataclasses import replace

    from tradebot.config import Mode

    service, _, key, fingerprint, _, name = setup
    req = service.request(fingerprint, "reason", name, "0.5")
    signature = sign(key, req)
    other = Store(tmp_path / "other.db")
    try:
        with pytest.raises(PermissionError, match="database mismatch"):
            SignedStrategyReduction(service.config, other, clock=SimClock(NOW)).apply(req, signature)
        live = replace(service.config, settings=service.config.settings.model_copy(update={"mode": Mode.LIVE}))
        with pytest.raises(PermissionError, match="mode mismatch"):
            SignedStrategyReduction(live, other, clock=SimClock(NOW)).apply(req, signature)
    finally:
        other.close()


def test_expiry_rechecked_after_database_lock(setup, monkeypatch):
    from contextlib import contextmanager

    service, store, key, fingerprint, _, name = setup
    req = service.request(fingerprint, "reason", name, "0.5", ttl=timedelta(seconds=1))
    original = store.transaction

    @contextmanager
    def delayed():
        with original() as conn:
            service.clock.advance(2)
            yield conn

    monkeypatch.setattr(store, "transaction", delayed)
    with pytest.raises(PermissionError, match="expired"):
        service.apply(req, sign(key, req))
    assert store.strategy_size_multiplier(name) == Decimal("1")
