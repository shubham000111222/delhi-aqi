"""Strategy pauses persist, cannot be shortened and block placement."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from tradebot.core.store import Store

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def test_concurrent_pause_extensions_keep_all_strategies_and_longest_deadline(tmp_path):
    path = tmp_path / "state.db"
    Store(path).close()

    def pause(item):
        name, seconds = item
        store = Store(path)
        try:
            store.extend_strategy_pause(name, until=NOW + timedelta(seconds=seconds), reason="risk", now=NOW)
        finally:
            store.close()

    items = [(f"S{i % 5}", i + 1) for i in range(50)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(pause, reversed(items)))
    store = Store(path)
    try:
        for i in range(5):
            name = f"S{i}"
            assert store.strategy_paused(name, now=NOW + timedelta(seconds=45 + i))
            assert not store.strategy_paused(name, now=NOW + timedelta(seconds=46 + i))
        assert set(store.kv_get("paused_strategies")) == {f"S{i}" for i in range(5)}
        assert store.count_events("strategy_paused") == len(store.autonomy_actions(limit=-1))
    finally:
        store.close()


@pytest.mark.parametrize("bad", [None, [], "bad", 1])
def test_corrupt_pause_map_blocks_all_strategies_and_is_not_overwritten(bad):
    store = Store(":memory:")
    store.kv_set("paused_strategies", bad)
    assert store.strategy_paused("S1", now=NOW)
    with pytest.raises(ValueError, match="invalid persisted"):
        store.extend_strategy_pause("S1", until=NOW + timedelta(hours=1), reason="test", now=NOW)
    assert store.kv_get("paused_strategies") == bad
    store.close()


@pytest.mark.parametrize("bad", [None, {}, {"until": None}, {"until": "invalid"}])
def test_indefinite_or_corrupt_strategy_hold_is_never_shortened(bad):
    store = Store(":memory:")
    store.kv_set("paused_strategies", {"S1": bad})
    assert store.strategy_paused("S1", now=NOW)
    assert not store.extend_strategy_pause("S1", until=NOW + timedelta(hours=1), reason="test", now=NOW)
    assert store.kv_get("paused_strategies")["S1"] == bad
    assert store.count_events("strategy_paused") == 0
    store.close()


def test_size_reductions_are_atomic_monotonic_and_audited(tmp_path):
    from decimal import Decimal

    path = tmp_path / "size.db"
    Store(path).close()

    def reduce(value):
        store = Store(path)
        try:
            store.reduce_strategy_size("S1", Decimal(value), now=NOW, reason="losses")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(reduce, ["0.5", "0.1", "0.3", "0.8", "0.2"]))
    store = Store(path)
    assert store.strategy_size_multiplier("S1") == Decimal("0.1")
    assert not store.reduce_strategy_size("S1", Decimal("0.5"), now=NOW, reason="later")
    assert store.count_events("size_reduction") == len(store.autonomy_actions(limit=-1))
    store.close()


@pytest.mark.parametrize("bad", [None, 0.5, "NaN", "-1", "1.1", "bad"])
def test_corrupt_size_reduction_cannot_be_overwritten(bad):
    from decimal import Decimal

    store = Store(":memory:")
    store.kv_set("size_mult_S1", bad)
    with pytest.raises(ValueError):
        store.strategy_size_multiplier("S1")
    with pytest.raises(ValueError):
        store.reduce_strategy_size("S1", Decimal("0.5"), now=NOW, reason="losses")
    assert store.kv_get("size_mult_S1") == bad
    store.close()


def test_failed_reduction_audit_rolls_back():
    import sqlite3
    from decimal import Decimal

    store = Store(":memory:")
    store._exec("CREATE TRIGGER fail_reduction BEFORE INSERT ON events WHEN NEW.kind='size_reduction' "
                "BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.reduce_strategy_size("S1", Decimal("0.5"), now=NOW, reason="losses")
    assert store.strategy_size_multiplier("S1") == Decimal("1")
    assert store.autonomy_actions(limit=-1) == []
    store.close()
