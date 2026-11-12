"""Loss-streak transitions and risk actions share a SQLite transaction."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.store import Store

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def record(store, *, name="S1", loss=True, closed_at=NOW):
    return store.record_strategy_trade_risk(name, loss=loss, closed_at=closed_at, now=NOW,
                                            halve_at=4, pause_at=6, pause_hours=24)


def test_concurrent_losses_count_and_apply_protections(tmp_path):
    path = tmp_path / "risk.db"
    Store(path).close()

    def one(_):
        store = Store(path)
        try:
            return record(store)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(one, range(30))) == list(range(1, 31))
    store = Store(path)
    assert store.kv_get("consecutive_losses") == {"S1": 30}
    assert store.strategy_size_multiplier("S1") == Decimal("0.5")
    assert store.strategy_paused("S1", now=NOW)
    assert store.count_events("size_reduction") == 1
    assert store.count_events("strategy_paused") == 1
    store.close()


def test_late_or_equal_win_cannot_reset_newer_streak_and_reduction_stays_latched():
    store = Store(":memory:")
    for i in range(4):
        assert record(store, closed_at=NOW + timedelta(seconds=i)) == i + 1
    assert record(store, loss=False, closed_at=NOW + timedelta(seconds=3)) == 4
    assert record(store, loss=False, closed_at=NOW + timedelta(seconds=2)) == 4
    assert record(store, loss=False, closed_at=NOW + timedelta(seconds=4)) == 0
    assert store.strategy_size_multiplier("S1") == Decimal("0.5")
    assert record(store, closed_at=NOW) == 1  # conservative late loss
    store.close()


def test_legacy_streak_is_preserved():
    store = Store(":memory:")
    store.kv_set("consecutive_losses", {"S1": 3, "S2": 2})
    assert record(store) == 4
    assert store.kv_get("consecutive_losses") == {"S1": 4, "S2": 2}
    assert store.strategy_size_multiplier("S1") == Decimal("0.5")
    store.close()


@pytest.mark.parametrize("bad", [None, [], {"S1": True}, {"S1": -1}, {"S1": "3"}])
def test_invalid_streak_fails_closed_without_overwriting(bad):
    store = Store(":memory:")
    store.kv_set("consecutive_losses", bad)
    assert store.strategy_paused("S1", now=NOW)
    with pytest.raises(ValueError):
        record(store)
    assert store.kv_get("consecutive_losses") == bad
    store.close()


def test_failed_pause_audit_rolls_back_streak_and_size():
    store = Store(":memory:")
    store.kv_set("consecutive_losses", {"S1": 5})
    store._exec("CREATE TRIGGER fail_risk BEFORE INSERT ON events WHEN NEW.kind='strategy_paused' "
                "BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        record(store)
    assert store.kv_get("consecutive_losses") == {"S1": 5}
    assert store.strategy_size_multiplier("S1") == Decimal("1")
    assert not store.strategy_paused("S1", now=NOW)
    assert store.autonomy_actions(limit=-1) == []
    store.close()


@pytest.mark.parametrize("bad", [None, [], {"S1": "broken"}, {"S1": 1}])
def test_invalid_streak_timestamps_block_entry(bad):
    store = Store(":memory:")
    store.kv_set("consecutive_losses_latest", bad)
    assert store.strategy_paused("S1", now=NOW)
    with pytest.raises(ValueError):
        record(store)
    store.close()
