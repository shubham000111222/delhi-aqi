"""Watchdog: a separate process that acts when the bot stops responding.

It has its own clock and its own database handle. It watches for:

* a missing heartbeat (the bot is hung, crashed, or the machine slept);
* an operator KILL file;
* a temporary permission slip: it never trades by itself in paper mode, it only
  escalates the persisted kill level, which the bot (or the next start) obeys.

In live mode the same loop is meant to run with its own exchange client so it can
cancel orders and flatten positions even if the bot process is wedged. That path is
implemented against the adapter interface in :class:`LiveWatchdogActions` and is
exercised by the fire drill in `tradebot drill`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import Any

from ..config import Config
from ..core.clock import Clock, parse_iso, utcnow
from ..core.logging import get_logger, setup_logging
from ..core.models import KillLevel
from ..core.store import Store

log = get_logger(__name__)


class LiveWatchdogActions:
    """Venue-side actions the watchdog can take without the bot process."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def flatten(self) -> dict[str, Any]:
        """Cancel every order and sell every non-quote balance at market."""
        from ..core.models import OrderRequest, OrderType, Side

        cancelled = await self.adapter.cancel_all()
        balances = await self.adapter.get_balances()
        sold = 0
        for asset, qty in balances.items():
            if asset == self.adapter.quote or qty is None or qty <= 0:
                continue
            symbol = f"{asset}/{self.adapter.quote}"
            try:
                rules = self.adapter.get_market_rules(symbol)
            except KeyError:
                continue  # not a symbol we know how to price: leave it, alert instead
            await self.adapter.place_order(
                OrderRequest(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=qty,
                    type=OrderType.MARKET,
                    strategy="watchdog",
                    tag="watchdog-flatten",
                    reduce_only=True,
                    reason=f"watchdog flatten of {symbol}",
                )
            )
            del rules
            sold += 1
        return {"cancelled": cancelled, "positions_sold": sold}


class Watchdog:
    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        kill_file: Path | str | None = Path("KILL"),
        actions: LiveWatchdogActions | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.cfg = config
        self.store = store or Store(config.settings.db_path)
        self.kill_file = Path(kill_file) if kill_file else None
        self.actions = actions
        self.clock = clock or Clock()
        self.interval = max(0.5, config.settings.watchdog.heartbeat_seconds / 2)
        self.lost_after = config.settings.watchdog.heartbeat_lost_seconds
        self.fired = 0

    # --- checks -----------------------------------------------------------
    def heartbeat_age(self) -> float:
        raw = self.store.kv_get("heartbeat")
        if not raw:
            return float("inf")
        try:
            return (utcnow() - parse_iso(str(raw))).total_seconds()
        except ValueError:  # pragma: no cover
            return float("inf")

    def lost(self) -> bool:
        return self.heartbeat_age() > self.lost_after

    def kill_file_present(self) -> bool:
        return bool(self.kill_file and self.kill_file.exists())

    def act(self) -> dict[str, Any]:
        """One watchdog pass. Returns what it did (empty dict = all quiet)."""
        if self.kill_file_present():
            reason = (self.kill_file.read_text().strip()[:200] if self.kill_file else "KILL file")
            self._escalate(KillLevel.L2, "operator_kill_file", reason or "KILL file present")
            return {"action": "kill_file", "level": "L2"}
        if self.lost():
            self._escalate(
                KillLevel.L2,
                "watchdog_heartbeat_lost",
                f"no heartbeat for {self.heartbeat_age():.0f}s (limit {self.lost_after}s)",
            )
            return {"action": "heartbeat_lost", "level": "L2"}
        return {}

    def _escalate(self, level: KillLevel, trigger: str, reason: str) -> None:
        current = KillLevel(self.store.kill_level())
        if current.rank >= level.rank:
            return
        self.store.set_kill_state(
            level.value, trigger=trigger, reason=reason, actor="watchdog", ts=utcnow()
        )
        self.store.add_incident(level.value, trigger, reason, {"actor": "watchdog"})
        self.fired += 1
        log.critical('{"event":"watchdog_escalation","level":"%s","trigger":"%s"}', level.value, trigger)

    async def act_async(self) -> dict[str, Any]:
        result = self.act()
        if result and self.actions is not None:
            try:
                result["flatten"] = await self.actions.flatten()
            except Exception as exc:  # noqa: BLE001
                result["flatten_error"] = str(exc)
                self.store.add_incident("CRIT", "watchdog_flatten_failed", str(exc))
        return result

    # --- loop -------------------------------------------------------------
    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        log.info('{"event":"watchdog_start","interval":%.2f}', self.interval)
        while stop_event is None or not stop_event.is_set():
            try:
                await self.act_async()
            except Exception:  # noqa: BLE001
                log.exception('{"event":"watchdog_error"}')
            await asyncio.sleep(self.interval)


def main() -> int:  # pragma: no cover - process entry point
    from ..config import load_config

    cfg = load_config()
    setup_logging(cfg.settings.logging.level, cfg.settings.logging.json_logs, "logs/watchdog.log")
    watch = Watchdog(cfg)
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        loop.run_until_complete(watch.run(stop))
    finally:
        loop.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def describe() -> dict[str, Any]:  # pragma: no cover - small helper for docs
    return {"component": "watchdog", "requires": "heartbeat kv key from the bot", "levels": ["L2", "L3"]}


__all__ = ["Watchdog", "LiveWatchdogActions", "describe"]
