"""Watchdog: a separate process that acts when the bot stops responding.

It has its own clock and its own database handle. It watches for:

* a missing heartbeat (the bot is hung, crashed, or the machine slept);
* an operator KILL file;
* a temporary permission slip: it never trades by itself in paper mode, it only
  escalates the persisted kill level, which the bot (or the next start) obeys.

The CLI has no venue client. Live/testnet restarts are blocked until an independent venue
snapshot and the safety preflight can be verified. Existing adapters only expose free balances
and locally tracked orders, which are insufficient to prove the book is safe.

Three things were missing until §20.5's supervision PR, and all three were the kind that
only look fine because nobody has died yet:

* **It could not restart anything.** Escalating a kill level tells a human the bot is
  wedged; it does not un-wedge it. `Supervisor` restarts it, within a budget, after making
  the book safe first.
* **It told nobody.** Its escalation went into the same SQLite file the wedged bot writes
  to, and the only transport was Telegram. `AlertRouter` writes every alert to disk
  *before* any network call, then tries Telegram and an independent webhook, and queues
  what no remote channel accepted.
* **Nothing watched the heartbeat independently of the database.** `DeadMansSwitch` reads
  a file the bot touches on every tick, and latches when it goes stale.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..alerts.router import AlertRouter
from ..config import Config, Mode
from ..core.clock import Clock, parse_iso
from ..core.logging import get_logger, setup_logging
from ..core.models import KillLevel
from ..core.store import Store
from .deadman import DeadMansSwitch
from .supervisor import Supervisor

log = get_logger(__name__)


class LiveWatchdogActions:
    """Venue-side actions the watchdog can take without the bot process."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def unprotected(self) -> list[str]:
        """Symbols we hold with no resting protective order at the venue.

        The venue is the source of truth here, not the bot's ledger: the question this
        answers is "if the bot never comes back, what is naked right now". A stop that
        exists only in the dead process's memory does not protect anything.
        """
        from ..core.models import OrderType, Side

        if not getattr(self.adapter, "watchdog_snapshot_is_authoritative", False):
            raise RuntimeError("cannot verify total balances and venue orders independently")
        balances = await self.adapter.get_balances()
        open_orders = await self.adapter.get_open_orders()
        naked: list[str] = []
        for asset, qty in balances.items():
            if asset == self.adapter.quote or qty is None or qty <= 0:
                continue
            symbol = f"{asset}/{self.adapter.quote}"
            coverage = sum(
                (order.remaining for order in open_orders
                 if order.symbol == symbol and order.side is Side.SELL
                 and getattr(order, "reduce_only", False)
                 and order.type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
                 and order.status.is_open),
                start=qty * 0,
            )
            if coverage < qty:
                naked.append(symbol)
        return naked

    async def flatten_unprotected(self) -> str:
        """Flatten only what has no stop. Returns a one-line detail for the audit trail."""
        from ..core.models import OrderRequest, OrderType, Side

        naked = await self.unprotected()
        if not naked:
            return "no unprotected position: nothing to flatten"
        sold = 0
        for symbol in naked:
            asset, _, quote = symbol.partition("/")
            qty = (await self.adapter.get_balances()).get(asset, 0)
            if qty is None or qty <= 0:
                continue
            # Selling while a partial stop is still open can over-close on venues
            # that do not enforce reduce-only. Refuse and escalate instead.
            orders = await self.adapter.get_open_orders(symbol)
            if any(order.side is Side.SELL and order.status.is_open for order in orders):
                raise RuntimeError(f"{symbol}: existing sell orders prevent safe flatten")
            self.adapter.get_market_rules(symbol)  # unknown market must block, not be skipped
            await self.adapter.place_order(
                OrderRequest(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=qty,
                    type=OrderType.MARKET,
                    strategy="watchdog",
                    tag="watchdog-unprotected",
                    reduce_only=True,
                    reason=f"flatten unprotected {symbol} before restart",
                )
            )
            remaining = (await self.adapter.get_balances()).get(asset, 0)
            if remaining is None or remaining > 0:
                raise RuntimeError(f"{symbol}: flatten not verified; remaining balance {remaining}")
            sold += 1
        del quote
        return f"flattened {sold} unprotected position(s): {', '.join(naked)}"

    async def flatten(self) -> dict[str, Any]:
        """Cancel every order and sell every non-quote balance at market."""
        from ..core.models import OrderRequest, OrderType, Side

        if not getattr(self.adapter, "watchdog_snapshot_is_authoritative", False):
            raise RuntimeError("cannot verify total balances and venue orders independently")
        cancelled = await self.adapter.cancel_all()
        if await self.adapter.get_open_orders():
            raise RuntimeError("cannot verify cancellation of all venue orders")
        balances = await self.adapter.get_balances()
        sold = 0
        for asset, qty in balances.items():
            if asset == self.adapter.quote or qty is None or qty <= 0:
                continue
            symbol = f"{asset}/{self.adapter.quote}"
            self.adapter.get_market_rules(symbol)  # failure means the book is not flat
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
            remaining = (await self.adapter.get_balances()).get(asset, 0)
            if remaining is None or remaining > 0:
                raise RuntimeError(f"{symbol}: flatten not verified; remaining balance {remaining}")
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
        router: AlertRouter | None = None,
        supervisor: Supervisor | None = None,
        deadman: DeadMansSwitch | None = None,
    ) -> None:
        self.cfg = config
        self.store = store or Store(config.settings.db_path)
        self.kill_file = Path(kill_file) if kill_file else None
        self.actions = actions
        self.clock = clock or Clock()
        self.interval = max(0.5, config.settings.watchdog.heartbeat_seconds / 2)
        self.lost_after = config.settings.watchdog.heartbeat_lost_seconds
        self.fired = 0
        wd = config.settings.watchdog
        beat = Path(wd.heartbeat_file)
        beat = beat if beat.is_absolute() else Path(config.settings.state_dir) / beat
        self.router = router
        self.supervisor = supervisor
        self.deadman = deadman or DeadMansSwitch(
            heartbeat_path=beat,
            stale_after=timedelta(seconds=wd.deadman_seconds),
            mode=config.settings.mode.value,
            clock=self.clock,
        )
        self.last: dict[str, Any] = {}

    # --- checks -----------------------------------------------------------
    def heartbeat_age(self) -> float:
        raw = self.store.kv_get("heartbeat")
        if not raw:
            return float("inf")
        try:
            return (self.clock.now() - parse_iso(str(raw))).total_seconds()
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
        transition_ids: list[int] = []
        tracking_failed = False
        try:
            changed = self.store.set_kill_state(
                level.value, trigger=trigger, reason=reason, actor="watchdog", ts=self.clock.now(),
                only_escalate=True, track_incident=True, transition_id_out=transition_ids,
            )
        except Exception:  # noqa: BLE001 - retry escalation with a latched tracking fault
            log.exception('{"event":"watchdog_incident_outbox_failed"}')
            tracking_failed = True
            changed = self.store.set_kill_state(
                level.value, trigger=trigger, reason=reason, actor="watchdog", ts=self.clock.now(),
                only_escalate=True, incident_tracking_failed=True,
            )
        if not changed:
            return
        if not tracking_failed:
            try:
                self.store.add_incident(level.value, trigger, reason, {"actor": "watchdog"},
                                        ts=self.clock.now(), kill_generation=transition_ids[0])
            except Exception:  # noqa: BLE001 - pending outbox blocks resume while protection continues
                log.exception('{"event":"watchdog_incident_record_failed","generation":%d}', transition_ids[0])
        self.fired += 1
        log.critical('{"event":"watchdog_escalation","level":"%s","trigger":"%s"}', level.value, trigger)

    async def alert(self, text: str, *, level: str = "CRIT", key: str | None = None) -> bool:
        """Tell a human. Returns True only if a *remote* channel took it.

        The watchdog used to write its escalation into the store and stop there, which is
        the one thing a wedged bot can also do. This is the path that reaches a phone.
        """
        if self.router is None:
            log.critical('{"event":"watchdog_alert_undeliverable","text":"%s"}', text[:300])
            return False
        try:
            return await self.router.send(text, level=level, key=key)
        except Exception:  # noqa: BLE001 - alerting must never break supervision
            log.exception('{"event":"watchdog_alert_failed"}')
            return False

    async def act_async(self) -> dict[str, Any]:
        """One pass: dead man's switch, kill file, heartbeat, then restart if needed."""
        result: dict[str, Any] = {}

        # 1. The switch that assumes the bot cannot speak. It fires once and latches.
        if self.deadman.check():
            state = self.deadman.state()
            result["deadman"] = state
            detail = "dead man's switch tripped: " + (
                "heartbeat file missing"
                if state["heartbeat_age_seconds"] is None
                else f"no heartbeat for {state['heartbeat_age_seconds']:.0f}s"
            )
            self._escalate(KillLevel.L2, "deadman_switch", detail)
            if self.actions is not None:
                try:
                    result["flatten"] = await self.actions.flatten()
                except Exception as exc:  # noqa: BLE001
                    result["flatten_error"] = str(exc)
                    self.store.add_incident("CRIT", "deadman_flatten_failed", str(exc))
            await self.alert(
                f"DEAD MAN'S SWITCH: {detail}. Kill level raised to L2; positions flattened "
                f"{'ok' if 'flatten' in result else 'FAILED' if 'flatten_error' in result else 'not attempted'}. "
                "Reset requires the heartbeat to be fresh again.",
                key="deadman",
            )
            return result

        # 2. Operator KILL file and a lost heartbeat, as before.
        result = self.act()
        if result.get("action") == "kill_file":
            await self.alert(
                "Operator KILL file: the bot is paused at L2. Remove the file to resume.",
                key="kill_file",
            )
            return result

        if result.get("action") == "heartbeat_lost":
            age = self.heartbeat_age()
            await self.alert(
                f"No heartbeat for {age:.0f}s (limit {self.lost_after:.0f}s). Kill level raised to L2.",
                key="heartbeat_lost",
            )
            if self.supervisor is not None:
                # Capture the old pulse *after* stopping the old child and completing
                # preflight. A fresh-looking heartbeat from before the restart proves
                # nothing about the new process.
                baseline: tuple[Any, Any] = (None, None)

                async def preflight() -> str:
                    nonlocal baseline
                    detail = await self._preflight()
                    baseline = (self.store.kv_get("heartbeat"), self.deadman.last_beat())
                    return detail

                async def heartbeat_ok() -> bool:
                    raw = self.store.kv_get("heartbeat")
                    if raw is not None and raw != baseline[0]:
                        try:
                            age = (self.clock.now() - parse_iso(str(raw))).total_seconds()
                            if 0 <= age <= self.lost_after:
                                return True
                        except (TypeError, ValueError):
                            pass
                    beat = self.deadman.last_beat()
                    if beat is not None and beat != baseline[1]:
                        age = (self.clock.now() - beat).total_seconds()
                        return 0 <= age <= self.deadman.stale_after.total_seconds()
                    return False

                result["restart"] = await self.supervisor.handle_lost_heartbeat(
                    heartbeat_ok=heartbeat_ok,
                    preflight=preflight,
                )
                if result["restart"].get("action") == "stop_failed":
                    await self.alert(
                        "Watchdog restart blocked: old process is still alive; operator action required.",
                        key="restart_stop_failed",
                    )
                elif result["restart"].get("action") == "preflight_failed":
                    await self.alert(
                        "Watchdog restart blocked: safety preflight failed; operator action required. "
                        f"{result['restart'].get('detail')}", key="restart_preflight_failed",
                    )
                elif result["restart"].get("action") == "refused":
                    await self.alert(
                        "Watchdog will not restart the bot again this hour: "
                        f"{result['restart'].get('reason')}. A human is needed.",
                        key="restart_refused",
                    )
                elif result["restart"].get("verified") is False:
                    await self.alert(
                        "Watchdog restarted the bot but it never heartbeated back. Kill level L2.",
                        key="restart_failed",
                    )
            return result

        # 3. Nothing wrong: retry whatever alerting could not deliver last time.
        if self.router is not None:
            pending = await self.router.flush()
            if pending:
                result["alerts_flushed"] = pending
        self.last = result
        return result

    async def _preflight(self) -> str:
        """Make the book safe before a new process starts trading it."""
        if self.cfg.mode is not Mode.PAPER:
            # Current adapters expose free balances and locally tracked orders, not total
            # venue balances or independent open orders. Neither a stop nor a flat book
            # can be proven from that data. Refuse instead of starting into unknown risk.
            raise RuntimeError("live/testnet preflight unavailable: independent venue snapshot required")
        if self.actions is None:
            return "paper mode: no venue preflight required"
        return await self.actions.flatten_unprotected()

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
