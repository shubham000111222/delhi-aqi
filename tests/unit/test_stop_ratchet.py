"""Durable, monotonic software stop updates, not venue protection claims."""
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.models import Position
from tradebot.core.store import Store

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def position():
    return Position(symbol="BTC/USDT", strategy="S1", qty=Decimal("1"), avg_entry=Decimal("100"),
                    stop=Decimal("90"), target=Decimal("120"), initial_stop=Decimal("90"), opened_at=NOW)


def test_stop_ratchet_survives_restart_and_rejects_stale_writer(tmp_path):
    path = tmp_path / "state.db"
    one, two = Store(path), Store(path)
    one.save_position(position())
    assert one.tighten_trailing_stop("BTC/USDT", "S1", Decimal("95"), ts=NOW) == Decimal("95")
    assert two.tighten_trailing_stop("BTC/USDT", "S1", Decimal("98"), ts=NOW) == Decimal("98")
    assert one.tighten_trailing_stop("BTC/USDT", "S1", Decimal("96"), ts=NOW) == Decimal("98")
    assert one.count_events("trailing_stop_tightened") == 2
    one.close()
    two.close()
    restored = Store(path)
    assert restored.open_positions()[0].effective_stop == Decimal("98")
    restored.close()


def test_stop_and_audit_roll_back_together_on_audit_failure():
    store = Store(":memory:")
    store.save_position(position())
    store._exec("CREATE TRIGGER fail_stop BEFORE INSERT ON events WHEN NEW.kind='trailing_stop_tightened' "
                "BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        store.tighten_trailing_stop("BTC/USDT", "S1", Decimal("95"), ts=NOW)
    assert store.open_positions()[0].effective_stop == Decimal("90")
    assert store.count_events("trailing_stop_tightened") == 0
    store.close()


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1")])
def test_stop_ratchet_rejects_invalid_values(value):
    store = Store(":memory:")
    store.save_position(position())
    with pytest.raises(ValueError, match="finite positive"):
        store.tighten_trailing_stop("BTC/USDT", "S1", value, ts=NOW)
    assert store.open_positions()[0].effective_stop == Decimal("90")
    store.close()


def test_stop_ratchet_rejects_missing_position():
    store = Store(":memory:")
    with pytest.raises(ValueError, match="persisted open position"):
        store.tighten_trailing_stop("BTC/USDT", "S1", Decimal("95"), ts=NOW)
    store.close()


def test_stale_position_save_preserves_stronger_stops_and_updates_qty(tmp_path):
    one, two = Store(tmp_path / "state.db"), Store(tmp_path / "state.db")
    one.save_position(position())
    stale = one.open_positions()[0]
    two.tighten_trailing_stop("BTC/USDT", "S1", Decimal("98"), ts=NOW)
    stale.stop = Decimal("89")
    stale.qty = Decimal("0.5")
    one.save_position(stale)
    saved = two.open_positions()[0]
    assert saved.stop == Decimal("90") and saved.effective_stop == Decimal("98")
    assert stale.stop == Decimal("90") and stale.effective_stop == Decimal("98")
    assert saved.qty == Decimal("0.5")
    assert one.count_events("autonomy_refused") == 1
    one.close()
    two.close()


def test_tightened_base_stop_cannot_be_lowered():
    store = Store(":memory:")
    pos = position()
    store.save_position(pos)
    pos.stop = Decimal("95")
    store.save_position(pos)
    pos.stop = Decimal("92")
    store.save_position(pos)
    assert pos.stop == Decimal("95")
    assert store.open_positions()[0].stop == Decimal("95")
    assert store.count_events("autonomy_refused") == 1
    store.close()
