"""Durable flapping hold never gets shortened by concurrent observers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from tradebot.core.clock import parse_iso
from tradebot.core.store import Store

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def test_concurrent_hold_extensions_preserve_longest_deadline(tmp_path):
    path = tmp_path / "holds.db"
    Store(path).close()

    def write(seconds):
        store = Store(path)
        try:
            store.extend_flapping_hold(NOW + timedelta(seconds=seconds), [str(seconds)], ts=NOW)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, reversed(range(1, 33))))
    store = Store(path)
    assert parse_iso(store.kv_get("health_flapping_hold")["until"]) == NOW + timedelta(seconds=32)
    store.close()


@pytest.mark.parametrize("invalid", [{}, {"until": "bad"}, [], None, 0])
def test_invalid_hold_is_not_overwritten(invalid):
    store = Store(":memory:")
    store.kv_set("health_flapping_hold", invalid)
    store.extend_flapping_hold(NOW + timedelta(minutes=10), ["feed"], ts=NOW)
    assert store.kv_get("health_flapping_hold") == invalid
    store.close()
