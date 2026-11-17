"""Read-only, diagnostic comparison of an observation with persisted local state.

Not reconciliation evidence: venue data may be incomplete, local and venue reads are not
atomic, and balances may include external/manual activity. No results authorize trading.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from .venue_observation import ObservationError, _amount

_ACTIVE = frozenset({"NEW", "SUBMITTED", "PARTIALLY_FILLED"})
_TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})
_MAX_ROWS = 10000


def _local_snapshot(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        # mode=ro does not create a missing DB or update schema; ordinary WAL semantics
        # are retained (immutable=1 would silently ignore uncheckpointed WAL data).
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            statuses = conn.execute("SELECT DISTINCT status FROM orders").fetchall()
            if any(row["status"] not in _ACTIVE | _TERMINAL for row in statuses):
                raise ObservationError("local order status is invalid")
            positions = conn.execute(
                "SELECT symbol, strategy, qty FROM positions WHERE closed_at IS NULL LIMIT ?", (_MAX_ROWS + 1,)
            ).fetchall()
            orders = conn.execute(
                "SELECT symbol,client_order_id,exchange_order_id,qty,filled_qty,status FROM orders "
                "WHERE status IN ('NEW','SUBMITTED','PARTIALLY_FILLED') LIMIT ?", (_MAX_ROWS + 1,)
            ).fetchall()
            if len(positions) > _MAX_ROWS or len(orders) > _MAX_ROWS:
                raise ObservationError("local comparison exceeds row limit")
            return [dict(row) for row in positions], [dict(row) for row in orders]
        finally:
            conn.close()
    except ObservationError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ObservationError("cannot read local state database") from exc


def compare_local(observation: dict[str, Any], *, path: Path, quote: str) -> dict[str, Any]:
    """Report differences, but never report an account as verified or safe."""
    if not isinstance(observation, dict) or not isinstance(observation.get("balances"), dict) or not isinstance(observation.get("open_orders"), list):
        raise ObservationError("invalid venue observation for comparison")
    if not isinstance(quote, str) or not quote or "/" in quote:
        raise ObservationError("invalid quote asset")
    positions, local_orders = _local_snapshot(path)
    amounts: dict[str, Decimal] = defaultdict(Decimal)
    for pos in positions:
        symbol, strategy = pos["symbol"], pos["strategy"]
        if not isinstance(symbol, str) or symbol.count("/") != 1 or not isinstance(strategy, str) or not strategy:
            raise ObservationError("invalid local position identity")
        base, symbol_quote = symbol.split("/")
        if not base or symbol_quote != quote or ":" in symbol:
            raise ObservationError("local position is not a supported spot symbol")
        qty = Decimal(_amount(pos["qty"]))
        if qty <= 0:
            raise ObservationError("invalid local position quantity")
        # A large count or very different magnitudes must not silently round the sum.
        with localcontext() as context:
            context.prec = 210
            amounts[base] += qty
    balance_differences: list[dict[str, str | None]] = []
    assets = set(amounts) | {asset for asset in observation["balances"] if asset != quote}
    for asset in sorted(assets):
        qty = amounts.get(asset, Decimal(0))
        value = observation["balances"].get(asset)
        if not isinstance(value, dict) or value.get("total") is None:
            balance_differences.append({"asset": asset, "local_qty": str(qty), "venue_total": None,
                                        "issue": "venue balance not reported"})
            continue
        venue_qty = Decimal(_amount(value["total"]))
        if venue_qty != qty:
            balance_differences.append({"asset": asset, "local_qty": str(qty), "venue_total": str(venue_qty),
                                        "issue": "quantity differs"})

    local_by_client: dict[str, dict[str, Any]] = {}
    local_by_exchange: dict[tuple[str, str], dict[str, Any]] = {}
    for order in local_orders:
        client, symbol, exchange_id = order["client_order_id"], order["symbol"], order["exchange_order_id"]
        if not isinstance(client, str) or not client or not isinstance(symbol, str) or not symbol:
            raise ObservationError("invalid local order identity")
        qty, filled = Decimal(_amount(order["qty"])), Decimal(_amount(order["filled_qty"]))
        if qty <= 0 or filled > qty:
            raise ObservationError("invalid local order quantity")
        local_by_client[client] = order
        if exchange_id:
            if not isinstance(exchange_id, str) or (symbol, exchange_id) in local_by_exchange:
                raise ObservationError("ambiguous local exchange order identity")
            local_by_exchange[(symbol, exchange_id)] = order

    differences: list[dict[str, str | None]] = []
    matched: set[str] = set()
    for venue in observation["open_orders"]:
        if not isinstance(venue, dict):
            raise ObservationError("invalid venue order in comparison")
        symbol, oid, client = venue.get("symbol"), venue.get("id"), venue.get("client_order_id")
        if not isinstance(symbol, str) or not symbol or not isinstance(oid, str) or not oid:
            raise ObservationError("invalid venue order identity")
        local = local_by_exchange.get((symbol, oid))
        if local is None and isinstance(client, str) and client:
            local = local_by_client.get(client)
        if local is None:
            differences.append({"issue": "venue order not found locally", "symbol": symbol, "exchange_id": oid,
                                "client_id": client if isinstance(client, str) else None})
            continue
        local_client = local["client_order_id"]
        if local_client in matched:
            raise ObservationError("multiple venue orders match one local order")
        matched.add(local_client)
        if (local["symbol"] != symbol or (local["exchange_order_id"] and local["exchange_order_id"] != oid)
                or (client and client != local_client) or Decimal(_amount(local["qty"])) != Decimal(_amount(venue.get("amount")))
                or Decimal(_amount(local["filled_qty"])) != Decimal(_amount(venue.get("filled")))):
            differences.append({"issue": "order details differ", "symbol": symbol, "exchange_id": oid,
                                "client_id": local_client})
    for client, order in local_by_client.items():
        if client not in matched:
            differences.append({"issue": "local order not observed at venue", "symbol": order["symbol"],
                                "exchange_id": order["exchange_order_id"] or None, "client_id": client})
    return {"position_assets": {asset: str(value) for asset, value in sorted(amounts.items())},
            "position_count": len(positions), "local_open_order_count": len(local_orders),
            "balance_differences": balance_differences, "order_differences": differences,
            "comparison_verified": False, "usable_for_reset": False,
            "warning": "Diagnostic only: separate snapshots; pagination, cash, external holdings and fills are unverified."}
