"""Fault budgets are transactional across processes and survive restart."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from tradebot.core.store import Store

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def test_concurrent_faults_cannot_evade_disable(tmp_path):
    path = tmp_path / "faults.db"
    Store(path).close()

    def fault(_):
        store = Store(path)
        try:
            return store.record_strategy_fault("S1", "fault", now=NOW)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fault, range(8)))
    store = Store(path)
    assert results.count(False) == 3
    assert results.count(True) == 5
    assert store.strategy_disabled("S1")
    assert len(store.strategy_fault_attempts("S1", now=NOW)) == 3
    assert store.count_events("strategy_paused") == 3
    assert store.count_events("strategy_disabled") == 1
    store.close()


def test_expiry_and_backward_clock_cannot_erase_future_faults():
    store = Store(":memory:")
    assert not store.record_strategy_fault("S1", "fault", now=NOW)
    assert store.strategy_fault_paused("S1", now=NOW + timedelta(hours=1))
    assert not store.strategy_fault_paused("S1", now=NOW + timedelta(hours=1, microseconds=1))
    assert store.strategy_fault_paused("S1", now=NOW - timedelta(days=1))
    store.close()
