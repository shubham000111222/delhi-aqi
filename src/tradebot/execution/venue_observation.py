"""Read-only venue account observation, not reconciliation or reset evidence.

CCXT's account-wide open-orders call can be paginated, incomplete or venue-specific.
This module never infers completeness, never changes orders and never clears a hold.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..core.clock import Clock, iso


class ObservationError(RuntimeError):
    """The venue could not provide a safely interpretable observation."""


def _amount(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ObservationError("invalid venue amount")
    text = str(value)
    if len(text) > 100:
        raise ObservationError("invalid venue amount")
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount < 0 or abs(amount.adjusted()) > 100:
            raise ObservationError("invalid venue amount")
        return str(amount)
    except InvalidOperation as exc:
        raise ObservationError("invalid venue amount") from exc


def _balances(raw: Any) -> dict[str, dict[str, str | None]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("total"), dict) or not raw["total"]:
        raise ObservationError("venue did not return total balances")
    result: dict[str, dict[str, str | None]] = {}
    totals = raw["total"]
    free, used = raw.get("free"), raw.get("used")
    if not isinstance(free, dict) or not isinstance(used, dict) or len(totals) > 10000:
        raise ObservationError("venue balance breakdown is unavailable or too large")
    for asset, value in totals.items():
        if not isinstance(asset, str) or not asset or len(asset) > 100 or value is None:
            raise ObservationError("invalid venue balance asset or total")
        result[asset] = {"total": _amount(value),
                         "free": _amount(free[asset]) if free.get(asset) is not None else None,
                         "used": _amount(used[asset]) if used.get(asset) is not None else None}
    return dict(sorted(result.items()))


def _orders(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 10000:
        raise ObservationError("invalid or oversized venue open-order response")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for order in raw:
        if not isinstance(order, dict):
            raise ObservationError("invalid venue order")
        oid, symbol, status = order.get("id"), order.get("symbol"), order.get("status")
        if (not isinstance(oid, str) or not oid or len(oid) > 200
                or not isinstance(symbol, str) or not symbol or len(symbol) > 200
                or not isinstance(status, str) or not status or len(status) > 200):
            raise ObservationError("venue order identity or status missing")
        if status.lower() not in {"open", "new", "partially_filled"}:
            raise ObservationError("venue returned an order without an open status")
        identity = (symbol, oid)
        if identity in seen:
            raise ObservationError("duplicate venue order identity")
        seen.add(identity)
        client_id = order.get("clientOrderId")
        if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 200):
            raise ObservationError("invalid client order identity")
        amount, filled = _amount(order.get("amount")), _amount(order.get("filled"))
        if Decimal(amount) <= 0 or Decimal(filled) > Decimal(amount):
            raise ObservationError("invalid open-order amounts")
        result.append({"id": oid, "symbol": symbol, "status": status,
                       "client_order_id": client_id, "amount": amount, "filled": filled})
    return result


async def observe_account(exchange: Any, *, mode: str, clock: Clock, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Query balances and account-wide open orders directly, without changing venue state.

    Returned times are local observation times, not venue timestamps. Calls are separate
    and their results are not an atomic snapshot. API success is not completeness proof.
    """
    if mode not in ("live", "testnet"):
        raise ObservationError("venue observation requires live or testnet mode")
    capabilities = getattr(exchange, "has", None)
    if not isinstance(capabilities, dict) or capabilities.get("fetchBalance") is not True or capabilities.get("fetchOpenOrders") is not True:
        raise ObservationError("venue does not explicitly report both required capabilities")
    if not 0 < timeout_seconds <= 60:
        raise ObservationError("invalid observation timeout")
    try:
        balance_started: datetime = clock.now()
        raw_balances = await asyncio.wait_for(exchange.fetch_balance(), timeout=timeout_seconds)
        balance_finished: datetime = clock.now()
        balances = _balances(raw_balances)
        orders_started: datetime = clock.now()
        raw_orders = await asyncio.wait_for(exchange.fetch_open_orders(), timeout=timeout_seconds)
        orders_finished: datetime = clock.now()
        orders = _orders(raw_orders)
    except ObservationError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not reveal venue error payloads or credentials
        raise ObservationError("venue account observation failed or timed out") from exc
    return {"mode": mode, "balances": balances, "open_orders": orders,
            "balance_started_at": iso(balance_started), "balance_finished_at": iso(balance_finished),
            "orders_started_at": iso(orders_started), "orders_finished_at": iso(orders_finished),
            "orders_complete": False, "atomic_snapshot": False, "usable_for_reset": False,
            "warning": "Read-only observation only; order pagination/completeness and venue behavior are unverified."}
