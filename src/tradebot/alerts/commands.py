"""Inbound Telegram commands: a whitelist, a confirm step, and an audit line each.

Outbound alerts already existed; this is the other direction. It is deliberately small
and paranoid, because a chat message can now move money:

* only chat ids on the whitelist are answered — everything else is ignored *and logged*,
  never acknowledged, so an attacker learns nothing;
* destructive commands (flatten, kill, lockdown) need a second message confirming them,
  and the confirmation expires;
* every command is written to the event log with the chat id, whether it succeeded or not;
* the router knows nothing about Telegram. It takes text in and returns replies, so the
  same logic is testable offline and usable from any transport.

The long-poll loop lives at the bottom and needs a bot token and network; it is wired but
cannot be exercised from a sandbox without either.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Commands that change positions or the kill switch: these need a confirmation.
DESTRUCTIVE = frozenset({"flatten", "kill", "lockdown"})

HELP = (
    "/status - kill level, equity, open positions\n"
    "/pnl - today's P&L and realised P&L\n"
    "/positions - open positions with stops\n"
    "/report - build today's report\n"
    "/pause - stop new entries (L1)\n"
    "/resume - clear an L1 pause\n"
    "/flatten - close everything (asks to confirm)\n"
    "/kill - flatten and pause (asks to confirm)\n"
    "/lockdown - flatten and lock at L3 (asks to confirm)\n"
    "/help - this list"
)


@dataclass
class Reply:
    text: str
    ok: bool = True
    command: str = ""
    authorised: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "command": self.command, "text": self.text}


@dataclass
class Ops:
    """The engine operations a command can reach. Injected so this stays testable."""

    status: Callable[[], str]
    pnl: Callable[[], str]
    positions: Callable[[], str]
    report: Callable[[], str]
    pause: Callable[[str], str]
    resume: Callable[[str], str]
    flatten: Callable[[str], str]
    kill: Callable[[str], str]
    lockdown: Callable[[str], str] | None = None


@dataclass
class CommandRouter:
    ops: Ops
    whitelist: frozenset[int] | frozenset[str]
    log: Callable[[str, dict[str, Any]], None] = lambda kind, payload: None
    confirm_ttl_seconds: float = 120.0
    _pending: dict[Any, tuple[str, float]] = field(default_factory=dict)

    # --- entry point -----------------------------------------------------
    def handle(self, chat_id: Any, text: str) -> Reply:
        command, _, argument = text.strip().partition(" ")
        command = command.lower().lstrip("/")
        if "@" in command:  # "/status@my_bot" in group chats
            command, _, _ = command.partition("@")

        if chat_id not in self.whitelist:
            self.log(
                "command_rejected",
                {"chat_id": str(chat_id), "command": command, "reason": "not whitelisted"},
            )
            return Reply(text="", ok=False, command=command, authorised=False)

        self.log("command_received", {"chat_id": str(chat_id), "command": command})

        if command in ("", "help", "start"):
            return self._reply("help", HELP)

        if command == "confirm":
            return self._confirm(chat_id)

        if command in DESTRUCTIVE:
            self._pending[chat_id] = (command, time.monotonic() + self.confirm_ttl_seconds)
            return self._reply(
                command,
                f"{command} needs confirmation: reply /confirm within "
                f"{int(self.confirm_ttl_seconds)}s to proceed.",
                ok=True,
            )

        handlers = {
            "status": self.ops.status,
            "pnl": self.ops.pnl,
            "positions": self.ops.positions,
            "report": self.ops.report,
        }
        if command in handlers:
            try:
                body = handlers[command]()
            except Exception as exc:  # noqa: BLE001 - report the failure, do not crash the bot
                return self._reply(command, f"{command} failed: {exc}", ok=False)
            return self._reply(command, body)

        control = {"pause": self.ops.pause, "resume": self.ops.resume}
        if command in control:
            reason = argument.strip() or f"telegram:{chat_id}"
            try:
                return self._reply(command, control[command](reason))
            except Exception as exc:  # noqa: BLE001
                return self._reply(command, f"{command} failed: {exc}", ok=False)

        return self._reply("unknown", f"unknown command /{command}\n\n{HELP}", ok=False)

    # --- helpers ---------------------------------------------------------
    def _confirm(self, chat_id: Any) -> Reply:
        pending = self._pending.pop(chat_id, None)
        if pending is None:
            return self._reply("confirm", "nothing to confirm", ok=False)
        command, expires = pending
        if time.monotonic() > expires:
            return self._reply("confirm", f"{command} confirmation expired; ask again", ok=False)
        reason = f"telegram-confirmed:{chat_id}"
        runner = {"flatten": self.ops.flatten, "kill": self.ops.kill, "lockdown": self.ops.lockdown}.get(command)
        if runner is None:
            return self._reply(command, f"{command} is not available", ok=False)
        try:
            return self._reply(command, runner(reason))
        except Exception as exc:  # noqa: BLE001
            return self._reply(command, f"{command} failed: {exc}", ok=False)

    def _reply(self, command: str, text: str, *, ok: bool = True) -> Reply:
        self.log("command", {"command": command, "ok": ok})
        return Reply(text=text, ok=ok, command=command)

    def pending_for(self, chat_id: Any) -> str | None:
        entry = self._pending.get(chat_id)
        return entry[0] if entry else None


def build_ops(
    *,
    engine: Any,
    store: Any,
    clock: Any,
    report_fn: Callable[[], Any] | None = None,
) -> Ops:
    """Wire the router to a live engine without giving it the engine itself."""

    def status() -> str:
        kill = store.kill_level()
        equity = engine.equity()
        return (
            f"kill {kill} | equity {equity:.2f} | open {len(engine.ledger.open_positions)} "
            f"| market time {clock.now().isoformat(timespec='seconds')}"
        )

    def pnl() -> str:
        ledger = engine.ledger
        return (
            f"realised {ledger.realized_pnl:+.2f} | unrealised "
            f"{ledger.unrealized(engine.marks):+.2f} | fees {ledger.fees_paid:.2f}"
        )

    def positions() -> str:
        open_positions = engine.ledger.open_positions
        if not open_positions:
            return "flat - no open positions"
        lines = [
            f"{p.symbol} {p.strategy} qty {p.qty} entry {p.avg_entry:.2f} stop {p.stop:.2f}"
            for p in open_positions
        ]
        return "\n".join(lines)

    tasks: set[Any] = set()

    def schedule(coro: Any, action: str) -> None:
        import asyncio

        task = asyncio.get_running_loop().create_task(coro)
        tasks.add(task)

        def completed(done: Any) -> None:
            tasks.discard(done)
            if done.cancelled():
                store.log_event("telegram_action_failed", {"action": action, "error": "cancelled"},
                                level="WARN", actor="telegram")
                return
            error = done.exception()
            if error is not None:
                store.log_event("telegram_action_failed", {"action": action, "error": type(error).__name__},
                                level="CRIT", actor="telegram")
            else:
                store.log_event("telegram_action_completed", {"action": action}, actor="telegram")

        task.add_done_callback(completed)

    def report() -> str:
        if report_fn is None:
            schedule(engine.emit_daily_report(), "report")
            return "report requested; check the event log for completion"
        result = report_fn()
        paths = result.get("paths", {}) if isinstance(result, dict) else {}
        return "report written: " + ", ".join(str(p) for p in paths.values())

    def pause(reason: str) -> str:
        from ..core.models import KillLevel

        engine.killswitch.trigger(KillLevel.L1, trigger="manual", reason=reason, actor="telegram")
        return f"paused at L1 ({reason})"

    def resume(reason: str) -> str:
        from ..core.models import KillLevel

        reviewed = engine.killswitch.state
        if reviewed.get("level") != KillLevel.L1.value:
            raise PermissionError("remote resume may only clear L1; L2/L3 require local review")
        engine.killswitch.reset(reason=reason, actor="telegram", expected_state=reviewed)
        return f"resumed to L0 ({reason})"

    def flatten(reason: str) -> str:
        from ..core.models import KillLevel

        engine.killswitch.trigger(KillLevel.L1, trigger="manual_flatten", reason=reason, actor="telegram")
        schedule(engine.flatten_all(reason), "flatten")
        return f"flatten requested; entries paused, check the event log for completion ({reason})"

    def kill(reason: str) -> str:
        from ..core.models import KillLevel

        engine.killswitch.trigger(KillLevel.L2, trigger="manual_kill", reason=reason, actor="telegram")
        flatten(reason)
        return f"kill requested: L2 and flatten ({reason})"

    def lockdown(reason: str) -> str:
        from ..core.models import KillLevel

        engine.killswitch.trigger(KillLevel.L3, trigger="manual_lockdown", reason=reason, actor="telegram")
        flatten(reason)
        return f"lockdown requested: L3 and flatten ({reason})"

    return Ops(
        status=status,
        pnl=pnl,
        positions=positions,
        report=report,
        pause=pause,
        resume=resume,
        flatten=flatten,
        kill=kill,
        lockdown=lockdown,
    )


def from_update(update: dict[str, Any]) -> tuple[Any, str] | None:
    """Extract ``(chat_id, text)`` from a Telegram update, or None for other updates."""
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    text = message.get("text")
    if text is None or "id" not in chat:
        return None
    return chat["id"], str(text)


def poll_once(transport: Any, router: CommandRouter, *, timeout: int = 25) -> int:
    """One long-poll cycle: fetch updates, answer each. Returns how many were answered.

    ``transport`` needs ``get_updates(timeout)`` and ``send_message(chat_id, text)``; the
    real one is a thin requests wrapper, a test can pass a fake.
    """
    answered = 0
    for update in transport.get_updates(timeout=timeout):
        parsed = from_update(update)
        if parsed is None:
            continue
        chat_id, text = parsed
        reply = router.handle(chat_id, text)
        if reply.text:
            transport.send_message(chat_id, reply.text)
        answered += 1
    return answered
