"""Tests for the inbound command router: whitelist, confirmation, audit."""

from __future__ import annotations

from typing import Any

import pytest

from tradebot.alerts.commands import CommandRouter, Ops, from_update, poll_once


def make_ops(calls: list[tuple[str, str]]) -> Ops:
    def record(name: str):
        def inner(reason: str = "") -> str:
            calls.append((name, reason))
            return f"{name} done"

        return inner

    return Ops(
        status=lambda: "kill L0 | equity 1000.00 | open 0",
        pnl=lambda: "realised +12.50 | unrealised -3.00",
        positions=lambda: "flat - no open positions",
        report=lambda: "report written: reports/paper/report.md",
        pause=record("pause"),
        resume=record("resume"),
        flatten=record("flatten"),
        kill=record("kill"),
    )


@pytest.fixture()
def wiring() -> tuple[CommandRouter, list[tuple[str, str]], list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, str]] = []
    log: list[tuple[str, dict[str, Any]]] = []
    router = CommandRouter(
        ops=make_ops(calls),
        whitelist=frozenset({111}),
        log=lambda kind, payload: log.append((kind, payload)),
    )
    return router, calls, log


OWNER = 111
STRANGER = 999


class TestWhitelist:
    def test_a_stranger_gets_no_answer_but_is_logged(self, wiring):
        router, calls, log = wiring
        reply = router.handle(STRANGER, "/kill")
        assert reply.text == "", "never acknowledge an unauthorised chat"
        assert reply.authorised is False
        assert calls == []
        assert ("command_rejected" in [kind for kind, _ in log])

    def test_the_owner_is_answered(self, wiring):
        router, _, _ = wiring
        reply = router.handle(OWNER, "/status")
        assert reply.authorised is True
        assert "kill L0" in reply.text


class TestCommands:
    def test_read_only_commands_work(self, wiring):
        router, _, _ = wiring
        assert "equity" in router.handle(OWNER, "/status").text
        assert "realised" in router.handle(OWNER, "/pnl").text
        assert "flat" in router.handle(OWNER, "/positions").text
        assert "report" in router.handle(OWNER, "/report").text

    def test_group_chat_suffixes_are_stripped(self, wiring):
        router, _, _ = wiring
        assert router.handle(OWNER, "/status@my_trading_bot").command == "status"

    def test_unknown_command_lists_the_help(self, wiring):
        router, _, _ = wiring
        reply = router.handle(OWNER, "/teleport")
        assert reply.ok is False
        assert "/status" in reply.text

    def test_pause_and_resume_are_immediate(self, wiring):
        router, calls, _ = wiring
        assert router.handle(OWNER, "/pause reason text").ok
        assert ("pause", "reason text") in calls
        assert router.handle(OWNER, "/resume").ok
        assert calls[-1][0] == "resume"


class TestConfirmation:
    def test_destructive_commands_need_a_second_message(self, wiring):
        router, calls, _ = wiring
        reply = router.handle(OWNER, "/flatten")
        assert reply.ok and "confirm" in reply.text.lower()
        assert calls == [], "nothing may happen on the first message"
        confirmed = router.handle(OWNER, "/confirm")
        assert confirmed.ok
        assert calls and calls[-1][0] == "flatten"

    def test_kill_flattens_only_after_confirmation(self, wiring):
        router, calls, _ = wiring
        router.handle(OWNER, "/kill")
        assert calls == []
        router.handle(OWNER, "/confirm")
        assert calls[-1][0] == "kill"

    def test_a_confirmation_cannot_be_reused(self, wiring):
        router, calls, _ = wiring
        router.handle(OWNER, "/flatten")
        router.handle(OWNER, "/confirm")
        second = router.handle(OWNER, "/confirm")
        assert second.ok is False
        assert len([c for c in calls if c[0] == "flatten"]) == 1

    def test_a_stranger_cannot_confirm_someone_elses_command(self, wiring):
        router, calls, _ = wiring
        router.handle(OWNER, "/flatten")
        router.handle(STRANGER, "/confirm")
        assert calls == []

    def test_expired_confirmation_is_refused(self, wiring):
        router, calls, _ = wiring
        router.confirm_ttl_seconds = -1  # already expired
        router.handle(OWNER, "/flatten")
        reply = router.handle(OWNER, "/confirm")
        assert reply.ok is False and "expired" in reply.text
        assert calls == []


class TestUpdates:
    def test_update_parsing_ignores_non_messages(self):
        assert from_update({"edited_message": {"chat": {"id": 1}}}) is None
        assert from_update({"message": {"chat": {"id": 5}, "text": "/status"}}) == (5, "/status")

    def test_poll_once_sends_each_reply(self, wiring):
        router, _, _ = wiring

        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[Any, str]] = []

            def get_updates(self, timeout: int = 25) -> list[dict[str, Any]]:
                return [
                    {"message": {"chat": {"id": OWNER}, "text": "/status"}},
                    {"message": {"chat": {"id": STRANGER}, "text": "/kill"}},
                ]

            def send_message(self, chat_id: Any, text: str) -> None:
                self.sent.append((chat_id, text))

        transport = FakeTransport()
        answered = poll_once(transport, router)
        assert answered == 2
        # the stranger is processed but never receives a message
        assert [chat for chat, _ in transport.sent] == [OWNER]
