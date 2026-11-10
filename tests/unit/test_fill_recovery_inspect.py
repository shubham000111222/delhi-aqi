from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.models import Fill, Side
from tradebot.core.store import Store
from tradebot.execution.fill_recovery import FillInspectionError, inspect_pending_fills


def fill():
    return Fill(order_id="order-1", symbol="BTC/USDT", side=Side.BUY, price=Decimal("100.123456789"),
                qty=Decimal("0.123456789"), fee=Decimal("0.00001"), ts=datetime(2024, 1, 1, tzinfo=UTC),
                strategy="S1", expected_price=Decimal("100"))


def test_inspect_pending_committed_wal_without_modification(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    identifier = store.journal_pending_fill(fill())
    result = inspect_pending_fills(path)
    assert result["pending_count"] == 1
    assert result["pending"][0]["callback_id"] == identifier
    assert result["pending"][0]["fill"]["price"] == "100.123456789"
    assert result["verified"] is result["usable_for_reset"] is False
    assert store.has_pending_fill()
    store.add_fill(fill(), pending_id=identifier)
    assert inspect_pending_fills(path)["pending_count"] == 0
    store.close()


def test_missing_db_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FillInspectionError):
        inspect_pending_fills(path)
    assert not path.exists()


@pytest.mark.parametrize("bad", [None, {}, {"bad": 1}, "x" * 70000])
def test_corrupt_or_oversized_journal_fails_without_clearing(tmp_path, bad):
    path = tmp_path / "state.db"
    store = Store(path)
    identifier = store.journal_pending_fill(fill())
    store.kv_set(f"fill_pending:{identifier}", bad)
    with pytest.raises(FillInspectionError):
        inspect_pending_fills(path)
    assert store.has_pending_fill()
    store.close()


def test_invalid_key_fails(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.kv_set("fill_pending:invalid", {})
    with pytest.raises(FillInspectionError):
        inspect_pending_fills(path)
    assert store.has_pending_fill()
    store.close()
