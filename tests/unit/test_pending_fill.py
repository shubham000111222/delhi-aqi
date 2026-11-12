"""Fill callback journal is a recovery hold, not verified venue history."""
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.config import Mode
from tradebot.core.clock import SimClock
from tradebot.core.models import Fill, KillLevel, Side
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def fill():
    return Fill(order_id="order1", symbol="BTC/USDT", side=Side.BUY, price=Decimal("100.123456789"),
                qty=Decimal("1"), fee=Decimal("0.1"), ts=NOW, strategy="S1", expected_price=Decimal("100"))


def test_pending_survives_restart_and_blocks_entries_and_reset(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    identifier = store.journal_pending_fill(fill())
    store.close()
    store = Store(path)
    switch = KillSwitch(store, clock=SimClock(NOW), kill_file=None, mode=Mode.PAPER)
    assert store.has_pending_fill()
    assert not switch.can_open_entries()
    switch.trigger(KillLevel.L1, trigger="test", reason="test")
    with pytest.raises(PermissionError):
        switch.reset(reason="test")
    assert store.has_pending_fill()
    store.add_fill(fill(), pending_id=identifier)
    assert not store.has_pending_fill()
    assert len(store.fills()) == 1
    switch.reset(reason="test")
    assert switch.can_open_entries()
    store.close()


def test_multiple_pending_callbacks_do_not_clear_each_other():
    store = Store(":memory:")
    a = store.journal_pending_fill(fill())
    b = store.journal_pending_fill(fill())
    assert a != b  # not venue identifiers; duplicate callbacks cannot be deduplicated
    store.add_fill(fill(), pending_id=a)
    assert store.has_pending_fill()
    store.add_fill(fill(), pending_id=b)
    assert not store.has_pending_fill()
    store.close()


def test_missing_or_changed_pending_cannot_be_cleared():
    store = Store(":memory:")
    identifier = store.journal_pending_fill(fill())
    store.kv_set(f"fill_pending:{identifier}", {"corrupt": True})
    with pytest.raises(ValueError, match="missing or changed"):
        store.add_fill(fill(), pending_id=identifier)
    assert store.has_pending_fill()
    assert not store.fills()
    store.close()


def test_fill_event_failure_keeps_hold_and_rolls_back_fill():
    store = Store(":memory:")
    identifier = store.journal_pending_fill(fill())
    store._exec("CREATE TRIGGER fail_fill BEFORE INSERT ON events WHEN NEW.kind='fill' "
                "BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_fill(fill(), pending_id=identifier)
    assert store.has_pending_fill()
    assert not store.fills()
    store.close()


def test_pending_journal_audit_failure_rolls_back():
    store = Store(":memory:")
    store._exec("CREATE TRIGGER fail_pending BEFORE INSERT ON events WHEN NEW.kind='fill_pending' "
                "BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.journal_pending_fill(fill())
    assert not store.has_pending_fill()
    store.close()
