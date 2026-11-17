"""Diagnostic comparison reads WAL-backed local state but never authorizes recovery."""
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradebot.core.models import Order, OrderStatus, OrderType, Position, Side
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.execution.venue_comparison import compare_local
from tradebot.execution.venue_observation import ObservationError

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def local_store(path: Path):
    store = Store(path)
    for strategy, qty in (("S1", "0.2"), ("S2", "0.3")):
        store.save_position(Position(symbol="BTC/USDT", strategy=strategy, qty=D(qty), avg_entry=D("100"),
                                     stop=D("90"), target=D("110"), initial_stop=D("90"), opened_at=NOW))
    store.upsert_order(Order(client_order_id="client1", exchange_order_id="venue1", symbol="BTC/USDT",
                             side=Side.BUY, type=OrderType.LIMIT, qty=D("0.1"), price=D("100"),
                             filled_qty=D("0.02"), status=OrderStatus.PARTIALLY_FILLED))
    return store


def observation():
    return {"balances": {"USDT": {"total": "200"}, "BTC": {"total": "0.5"}},
            "open_orders": [{"id": "venue1", "symbol": "BTC/USDT", "status": "open",
                             "client_order_id": "client1", "amount": "0.1", "filled": "0.02"}]}


def test_matching_observation_is_not_verified(tmp_path):
    path = tmp_path / "state.db"
    store = local_store(path)  # keep WAL writer open; read must include its committed WAL
    try:
        result = compare_local(observation(), path=path, quote="USDT")
        assert result["position_assets"] == {"BTC": "0.5"}
        assert not result["balance_differences"] and not result["order_differences"]
        assert not result["comparison_verified"] and not result["usable_for_reset"]
        assert len(store.open_positions()) == 2 and len(store.open_orders()) == 1
    finally:
        store.close()


def test_differences_are_reported_without_modifying_database(tmp_path):
    path = tmp_path / "state.db"
    store = local_store(path)
    obs = observation()
    obs["balances"]["BTC"]["total"] = "0.4"
    obs["balances"]["ETH"] = {"total": "1"}
    obs["open_orders"] = [{"id": "other", "symbol": "ETH/USDT", "client_order_id": None,
                           "amount": "1", "filled": "0"}]
    try:
        result = compare_local(obs, path=path, quote="USDT")
        assert {item["asset"] for item in result["balance_differences"]} == {"BTC", "ETH"}
        assert {item["issue"] for item in result["order_differences"]} == {
            "venue order not found locally", "local order not observed at venue"}
        assert len(store.open_positions()) == 2 and len(store.open_orders()) == 1
        assert not result["usable_for_reset"]
    finally:
        store.close()


def test_missing_balance_and_mismatched_order_details(tmp_path):
    path = tmp_path / "state.db"
    store = local_store(path)
    obs = observation()
    del obs["balances"]["BTC"]
    obs["open_orders"][0]["filled"] = "0.03"
    try:
        result = compare_local(obs, path=path, quote="USDT")
        assert result["balance_differences"][0]["issue"] == "venue balance not reported"
        assert result["order_differences"][0]["issue"] == "order details differ"
    finally:
        store.close()


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(ObservationError, match="cannot read"):
        compare_local(observation(), path=path, quote="USDT")
    assert not path.exists()


@pytest.mark.parametrize("update", ["UPDATE positions SET qty='NaN'", "UPDATE positions SET symbol='BTC/USDT:USDT'",
                                    "UPDATE orders SET status='UNKNOWN'", "UPDATE orders SET filled_qty='-2'"])
def test_corrupt_local_state_fails_closed(tmp_path, update):
    path = tmp_path / "state.db"
    store = local_store(path)
    store._exec(update)
    try:
        with pytest.raises(ObservationError):
            compare_local(observation(), path=path, quote="USDT")
    finally:
        store.close()
