"""Allocation ramp must not increase without authenticated action-bound approval."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tradebot.config import Mode
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.risk.live_guards import RampController, RampState

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def live(cfg):
    return replace(cfg, settings=cfg.settings.model_copy(update={"mode": Mode.LIVE}))


def test_live_multiplier_caps_legacy_unverified_ramp_state(cfg):
    store = Store(":memory:")
    store.kv_set("ramp", {"stage": 3, "critical_incidents": 0})
    assert RampController(live(cfg), store).multiplier() == D("0.10")
    store.close()


def test_direct_save_cannot_escalate_or_clear_incidents(cfg):
    store = Store(":memory:")
    ramp = RampController(live(cfg), store)
    ramp.save(RampState(stage_started=NOW))
    with pytest.raises(PermissionError, match="approval"):
        ramp.save(RampState(stage=1, stage_started=NOW))
    assert ramp.load().stage == 0
    assert store.count_events("autonomy_refused") == 1
    stale = ramp.load()
    ramp.register_incident()
    with pytest.raises(PermissionError, match="incidents"):
        ramp.save(stale)
    assert ramp.load().critical_incidents == 1
    assert store.count_events("autonomy_refused") == 2
    store.close()


def test_elapsed_time_does_not_authorize_ramp(cfg):
    store = Store(":memory:")
    ramp = RampController(live(cfg), store)
    ramp.save(RampState(stage_started=NOW))
    assert ramp.advance_if_eligible(NOW + timedelta(days=100)).stage == 0
    assert ramp.multiplier() == D("0.10")
    assert store.count_events("autonomy_refused") == 1
    store.close()


@pytest.mark.parametrize("raw", [{"stage": 4}, {"stage": -1}, {"stage": "3"},
                                  {"critical_incidents": -1}, {"stage_started": "bad"}, []])
def test_corrupt_ramp_blocks_live_sizing(cfg, raw):
    store = Store(":memory:")
    store.kv_set("ramp", raw)
    with pytest.raises(ValueError, match="ramp"):
        RampController(live(cfg), store).multiplier()
    store.close()


def test_initial_configured_ramp_can_be_stricter_but_not_looser(cfg):
    store = Store(":memory:")
    config = live(cfg)
    config.settings.live_guards.ramp_multipliers = (D("0.05"), D("1"))
    assert RampController(config, store).multiplier() == D("0.05")
    config.settings.live_guards.ramp_multipliers = (D("0.5"), D("1"))
    assert RampController(config, store).multiplier() == D("0.10")
    store.close()


def test_concurrent_incidents_are_not_lost(tmp_path, cfg):
    path = tmp_path / "ramp.db"
    root = Store(path)
    root.close()

    def add_incident(_):
        store = Store(path)
        try:
            RampController(live(cfg), store).register_incident()
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_incident, range(20)))
    store = Store(path)
    assert RampController(live(cfg), store).load().critical_incidents == 20
    assert store.count_events("ramp_incident") == 20
    store.close()


def test_incident_audit_failure_rolls_back_increment(cfg):
    import sqlite3

    store = Store(":memory:")
    store._exec("CREATE TRIGGER fail_incident BEFORE INSERT ON events WHEN NEW.kind='ramp_incident' "
                "BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    ramp = RampController(live(cfg), store)
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        ramp.register_incident()
    assert ramp.load().critical_incidents == 0
    store.close()


@pytest.mark.parametrize("initial", [(), (D("NaN"),), (D("0"),), (D("-0.1"),), (D("1.1"),)])
def test_invalid_initial_multiplier_blocks_live(cfg, initial):
    store = Store(":memory:")
    config = live(cfg)
    config.settings.live_guards.ramp_multipliers = initial
    with pytest.raises(ValueError, match="multiplier"):
        RampController(config, store).multiplier()
    store.close()


@pytest.mark.parametrize("field", ["ramp_multiplier", "size_multiplier"])
@pytest.mark.parametrize("value", [D("1.01"), D("-0.1"), D("NaN"), D("Infinity")])
def test_sizing_rejects_invalid_risk_increasing_multipliers(cfg, field, value):
    from tradebot.core.models import MarketRules
    from tradebot.portfolio.sizing import size_entry

    rules = MarketRules("BTC/USDT", D("0.01"), D("0.00001"), D("0.00001"), D("5"), D("0.001"), D("0.001"))
    result = size_entry(price=D("100"), stop=D("90"), equity=D("1000"), starting_capital=D("1000"),
                        limits=cfg.risk, rules=rules, **{field: value})
    assert result.qty == 0
    assert result.binding == f"invalid_{field}"
