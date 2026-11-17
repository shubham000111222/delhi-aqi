"""Observations are direct, read-only, bounded and never certified as reset evidence."""
import asyncio
from datetime import UTC, datetime

import pytest

from tradebot.core.clock import SimClock
from tradebot.execution.venue_observation import ObservationError, observe_account

NOW = datetime(2024, 1, 1, tzinfo=UTC)


class Exchange:
    has = {"fetchBalance": True, "fetchOpenOrders": True}

    def __init__(self):
        self.calls = []
        self.balances = {"total": {"USDT": 100.0, "BTC": 0.5},
                         "free": {"USDT": 90.0, "BTC": 0.3}, "used": {"USDT": 10.0, "BTC": 0.2}}
        self.orders = [{"id": "123", "symbol": "BTC/USDT", "status": "open", "clientOrderId": "client-1",
                        "amount": 0.2, "filled": 0.1}]

    async def fetch_balance(self):
        self.calls.append("balance")
        return self.balances

    async def fetch_open_orders(self):
        self.calls.append("all-open-orders")
        return self.orders


async def test_query_is_direct_read_only_and_never_authorizes_reset():
    venue = Exchange()
    result = await observe_account(venue, mode="testnet", clock=SimClock(NOW))
    assert venue.calls == ["balance", "all-open-orders"]
    assert result["balances"]["BTC"] == {"total": "0.5", "free": "0.3", "used": "0.2"}
    assert result["open_orders"][0]["id"] == "123"
    assert result["orders_complete"] is False
    assert result["atomic_snapshot"] is False
    assert result["usable_for_reset"] is False


@pytest.mark.parametrize("has", [None, {}, {"fetchBalance": "true", "fetchOpenOrders": True},
                                 {"fetchBalance": True, "fetchOpenOrders": "emulated"}])
async def test_missing_or_emulated_capabilities_are_rejected(has):
    venue = Exchange()
    venue.has = has
    with pytest.raises(ObservationError, match="capabilities"):
        await observe_account(venue, mode="live", clock=SimClock(NOW))
    assert not venue.calls


@pytest.mark.parametrize("bad", [None, [], {}, {"total": {}, "free": {}, "used": {}},
                                 {"total": {"BTC": None}, "free": {}, "used": {}},
                                 {"total": {"BTC": "NaN"}, "free": {}, "used": {}},
                                 {"total": {"BTC": -1}, "free": {}, "used": {}},
                                 {"total": {"BTC": 1}, "free": None, "used": {}}])
async def test_malformed_balance_fails_without_claiming_observation(bad):
    venue = Exchange()
    venue.balances = bad
    with pytest.raises(ObservationError):
        await observe_account(venue, mode="live", clock=SimClock(NOW))
    assert venue.calls == ["balance"]


@pytest.mark.parametrize("orders", [None, {}, [None], [{"id": "1", "symbol": "BTC/USDT"}],
                                    [{"id": "1", "symbol": "BTC/USDT", "status": "closed", "amount": 1, "filled": 1}],
                                    [{"id": "1", "symbol": "BTC/USDT", "status": "open", "amount": 1, "filled": 2}]])
async def test_bad_orders_never_yield_a_snapshot(orders):
    venue = Exchange()
    venue.orders = orders
    with pytest.raises(ObservationError):
        await observe_account(venue, mode="live", clock=SimClock(NOW))


async def test_duplicate_open_order_ids_fail():
    venue = Exchange()
    venue.orders.append(dict(venue.orders[0]))
    with pytest.raises(ObservationError, match="duplicate"):
        await observe_account(venue, mode="testnet", clock=SimClock(NOW))


async def test_timeout_and_venue_errors_are_redacted():
    venue = Exchange()

    async def slow():
        await asyncio.sleep(1)

    venue.fetch_balance = slow
    with pytest.raises(ObservationError, match="timed out"):
        await observe_account(venue, mode="testnet", clock=SimClock(NOW), timeout_seconds=0.001)

    async def broken():
        raise ValueError("sensitive secret")

    venue.fetch_balance = broken
    with pytest.raises(ObservationError) as exc:
        await observe_account(venue, mode="testnet", clock=SimClock(NOW))
    assert "sensitive secret" not in str(exc.value)


async def test_paper_mode_is_refused_without_calling_venue():
    venue = Exchange()
    with pytest.raises(ObservationError, match="requires"):
        await observe_account(venue, mode="paper", clock=SimClock(NOW))
    assert not venue.calls
