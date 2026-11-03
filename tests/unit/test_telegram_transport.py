"""The Telegram transport, driven through an httpx MockTransport - no network, no token."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tradebot.alerts.commands import CommandRouter, Ops
from tradebot.alerts.telegram import TelegramTransport, serve, transport_from_config

TOKEN = "123456:TEST-TOKEN"
OWNER = 4242


def ops() -> Ops:
    return Ops(
        status=lambda: "kill level L0, equity 1000",
        pnl=lambda: "today +0",
        positions=lambda: "flat",
        report=lambda: "report",
        pause=lambda reason: "paused",
        resume=lambda reason: "resumed",
        flatten=lambda reason: "flat",
        kill=lambda reason: "killed",
    )


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def update(update_id: int, text: str, chat_id: int = OWNER) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


class TestUpdates:
    def test_a_poll_asks_for_updates_and_parses_them(self):
        seen: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content or b"{}"))
            assert request.url.path.endswith("/getUpdates")
            return httpx.Response(200, json={"ok": True, "result": [update(1, "/status")]})

        async def run() -> list[dict[str, Any]]:
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            return await transport.get_updates(timeout=1)

        updates = asyncio.run(run())
        assert updates == [update(1, "/status")]
        assert seen[0]["timeout"] == 1
        assert "offset" not in seen[0], "the first poll must not skip anything"

    def test_the_offset_is_sent_once_it_is_known(self):
        seen: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"ok": True, "result": []})

        async def run() -> None:
            transport = TelegramTransport(token=TOKEN, client=client(handler), offset=99)
            await transport.get_updates(timeout=1)

        asyncio.run(run())
        assert seen[0]["offset"] == 99

    def test_a_http_error_returns_no_updates_and_is_counted(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, json={})

        async def run():
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            updates = await transport.get_updates(timeout=1)
            return updates, transport.errors

        updates, errors = asyncio.run(run())
        assert updates == [] and errors == 1

    def test_an_ok_false_body_returns_no_updates(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

        async def run():
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            return await transport.get_updates(timeout=1), transport.errors

        updates, errors = asyncio.run(run())
        assert updates == [] and errors == 1

    def test_acknowledging_advances_past_the_batch(self):
        transport = TelegramTransport(token=TOKEN)
        assert transport.offset is None
        transport.acknowledge([update(7, "/status"), update(9, "/help")])
        assert transport.offset == 10
        transport.acknowledge([])
        assert transport.offset == 10

    def test_a_missing_token_is_refused_before_any_request(self):
        with pytest.raises(ValueError, match="bot token"):
            TelegramTransport(token="")


    def test_a_network_fault_returns_nothing_instead_of_raising(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        async def run():
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            updates = await transport.get_updates(timeout=1)
            return updates, transport.errors

        updates, errors = asyncio.run(run())
        assert updates == [] and errors == 1


class TestSending:
    def test_a_reply_is_addressed_and_truncated(self):
        seen: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        async def run() -> bool:
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            ok = await transport.send_message(OWNER, "x" * 9000)
            assert len(transport.sent) == 1
            return ok

        assert asyncio.run(run()) is True
        assert seen[0]["chat_id"] == OWNER
        assert len(seen[0]["text"]) == 4000, "Telegram rejects anything longer"

    def test_a_telegram_rejection_is_reported_not_raised(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"ok": False})

        async def run():
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            return await transport.send_message(OWNER, "hello"), transport.sent

        ok, sent = asyncio.run(run())
        assert ok is False and sent == []


class TestServeLoop:
    def test_it_answers_the_owner_and_stays_silent_to_strangers(self):
        sent: list[dict[str, Any]] = []
        batch = [update(1, "/status"), update(2, "/kill", chat_id=666)]

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getUpdates"):
                return httpx.Response(200, json={"ok": True, "result": batch})
            body = json.loads(request.content or b"{}")
            sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        router = CommandRouter(ops=ops(), whitelist=frozenset({OWNER}))

        async def run() -> dict[str, Any]:
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            return await serve(router, transport, poll_timeout=1, stop_after=1)

        report = asyncio.run(run())
        assert report["answered"] == 2, "both messages are handled, one is refused inside"
        assert len(sent) == 1 and sent[0]["chat_id"] == OWNER
        assert "kill level" in sent[0]["text"]
        assert report["offset"] == 3, "the batch is acknowledged only after it is handled"

    def test_a_poll_that_raises_does_not_kill_the_listener(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"ok": True, "result": []})

        router = CommandRouter(ops=ops(), whitelist=frozenset({OWNER}))

        async def run() -> dict[str, Any]:
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            return await serve(router, transport, poll_timeout=1, max_backoff=0.0, stop_after=2)

        report = asyncio.run(run())
        assert report["polls"] == 2
        assert report["errors"] == 1

    def test_a_one_use_confirmation_survives_two_polls(self):
        """The pending confirm lives in the router, not in the update batch."""
        batches = [[update(1, "/kill")], [update(2, "/confirm")]]
        sent: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getUpdates"):
                return httpx.Response(200, json={"ok": True, "result": batches.pop(0)})
            sent.append(json.loads(request.content or b"{}")["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        router = CommandRouter(ops=ops(), whitelist=frozenset({OWNER}))

        async def run() -> None:
            transport = TelegramTransport(token=TOKEN, client=client(handler))
            await serve(router, transport, poll_timeout=1, stop_after=2)

        asyncio.run(run())
        assert any("confirm" in text.lower() for text in sent), sent
        assert any("killed" in text.lower() for text in sent), sent


class TestConfig:
    def test_no_token_means_no_transport(self):
        class Secrets:
            telegram_bot_token = ""

        class Config:
            secrets = Secrets()

        assert transport_from_config(Config()) is None

    def test_a_token_means_a_transport(self):
        class Secrets:
            telegram_bot_token = TOKEN

        class Config:
            secrets = Secrets()

        transport = transport_from_config(Config())
        assert isinstance(transport, TelegramTransport)
        assert transport.token == TOKEN
