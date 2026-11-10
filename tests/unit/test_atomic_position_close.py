"""The normal position-close path commits its loss risk atomically."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Position
from tradebot.core.store import Store

NOW = datetime(2024, 1, 1, tzinfo=UTC)
D = Decimal


def position():
    return Position(symbol="BTC/USDT", strategy="S1", qty=D("1"), avg_entry=D("100"), stop=D("90"),
                    target=D("120"), initial_stop=D("90"), opened_at=NOW, initial_qty=D("1"))


def close(store, pos):
    return store.close_position_with_risk(pos, D("95"), "stop", closed_at=NOW + timedelta(hours=1), now=NOW,
                                          halve_at=4, pause_at=6, pause_hours=24)


def closed_position():
    pos = position()
    pos.qty = D("0")
    pos.realized_pnl = D("-5")
    return pos


def test_close_commits_streak_pause_and_position_together():
    store = Store(":memory:")
    store.save_position(position())
    store.kv_set("consecutive_losses", {"S1": 5})
    close(store, closed_position())
    assert not store.open_positions()
    assert len(store.closed_positions()) == 1
    assert store.kv_get("consecutive_losses") == {"S1": 6}
    assert store.strategy_size_multiplier("S1") == D("0.5")
    assert store.strategy_paused("S1", now=NOW)
    with pytest.raises(ValueError, match="missing or changed"):
        close(store, closed_position())
    assert store.kv_get("consecutive_losses") == {"S1": 6}
    store.close()


def test_risk_audit_failure_rolls_back_close():
    store = Store(":memory:")
    store.save_position(position())
    store.kv_set("consecutive_losses", {"S1": 5})
    store._exec("CREATE TRIGGER fail_close_risk BEFORE INSERT ON events WHEN NEW.kind='strategy_paused' "
                "BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        close(store, closed_position())
    assert len(store.open_positions()) == 1
    assert not store.closed_positions()
    assert store.kv_get("consecutive_losses") == {"S1": 5}
    assert store.strategy_size_multiplier("S1") == D("1")
    assert not store.autonomy_actions(limit=-1)
    store.close()


def test_stale_close_cannot_close_new_position():
    store = Store(":memory:")
    newer = position()
    newer.opened_at += timedelta(days=1)
    store.save_position(newer)
    with pytest.raises(ValueError, match="missing or changed"):
        close(store, closed_position())
    assert len(store.open_positions()) == 1
    assert store.kv_get("consecutive_losses") is None
    store.close()


def test_competing_closures_only_count_once(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.save_position(position())
    store.close()

    def attempt(_):
        db = Store(path)
        try:
            try:
                close(db, closed_position())
                return True
            except ValueError:
                return False
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(12))) == 1
    store = Store(path)
    assert store.kv_get("consecutive_losses") == {"S1": 1}
    assert len(store.closed_positions()) == 1
    store.close()
