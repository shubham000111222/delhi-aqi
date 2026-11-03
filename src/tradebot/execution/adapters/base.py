"""The one interface every execution venue implements.

Strategy, risk and portfolio code only ever see this interface, so the same code
runs against paper, testnet and live. The paper adapter implements it exactly
(including latency, fees, book impact and simulated failures).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from ...config import Mode
from ...core.models import MarketRules, Order, OrderRequest


class ExchangeError(Exception):
    """Base class for adapter errors."""


class TransientExchangeError(ExchangeError):
    """Safe to retry: network, 429, 5xx."""


class PermanentExchangeError(ExchangeError):
    """Never retry: invalid size, insufficient funds, symbol halted."""


class ExchangeAdapter(ABC):
    mode: Mode = Mode.PAPER
    name: str = "adapter"

    # --- orders -----------------------------------------------------------
    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """Submit an order. Must be idempotent on client_order_id."""

    @abstractmethod
    async def cancel_order(self, client_order_id: str, symbol: str | None = None) -> bool:
        """Cancel one order. Returns True if it was open and is now cancelled."""

    @abstractmethod
    async def cancel_all(self, symbol: str | None = None) -> int:
        """Cancel every open order (optionally for one symbol). Returns the count."""

    @abstractmethod
    async def get_order(self, client_order_id: str) -> Order | None:
        """Query by client id. Used after timeouts instead of resending blindly."""

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]: ...

    # --- account ----------------------------------------------------------
    @abstractmethod
    async def get_balances(self) -> dict[str, Decimal]:
        """Free balances keyed by asset, e.g. {'USDT': 900, 'BTC': 0.001}."""

    @abstractmethod
    def get_market_rules(self, symbol: str) -> MarketRules: ...

    @abstractmethod
    def market_price(self, symbol: str) -> Decimal | None:
        """Last known price (mid if available). Never blocks."""

    # --- lifecycle --------------------------------------------------------
    async def close(self) -> None:  # pragma: no cover - optional
        return None

    def describe(self) -> dict[str, object]:
        return {"adapter": self.name, "mode": self.mode.value}
