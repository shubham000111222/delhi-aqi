"""The Telegram transport the command router was always written against.

`commands.py` deliberately knows nothing about Telegram: it takes `(chat_id, text)` and
returns a reply. This is the other half - `getUpdates` long polling in, `sendMessage` out
- and it is the piece that used to be missing.

Design notes, all of them consequences of what goes wrong in practice:

* **The offset is persisted in the object and advanced only after a batch is handled.**
  Telegram repeats every update until you acknowledge it by offset; advancing before
  handling means a crash mid-batch silently drops a `/flatten`.
* **A failed poll is not fatal.** The loop backs off (1s, 2s, 4s ... capped) and keeps
  going: the listener dying is worse than the listener being slow, because it is the
  only remote way to stop the bot.
* **Every HTTP call goes through an injected client**, so tests drive the whole loop
  against canned JSON and no network.
* **Nothing here logs the token.** Bot URLs contain it; the log lines carry the method
  and the status code only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.logging import get_logger

log = get_logger(__name__)

API = "https://api.telegram.org"


@dataclass
class TelegramTransport:
    """Long polling and message sending for one bot token."""

    token: str
    client: httpx.AsyncClient | None = None
    api: str = API
    timeout: float = 35.0
    offset: int | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    errors: int = 0

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("a telegram transport needs a bot token")

    def _url(self, method: str) -> str:
        return f"{self.api}/bot{self.token}/{method}"

    async def _call(self, method: str, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        self.calls.append(method)
        try:
            if self.client is not None:
                response = await self.client.post(
                    self._url(method), json=payload, timeout=timeout or self.timeout
                )
            else:  # pragma: no cover - needs network
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        self._url(method), json=payload, timeout=timeout or self.timeout
                    )
        except Exception as exc:  # noqa: BLE001 - a network fault is an error count, not a crash
            self.errors += 1
            log.warning('{"event":"telegram_unreachable","method":"%s","error":"%s"}', method, type(exc).__name__)
            return None
        if response.status_code != 200:
            self.errors += 1
            log.warning('{"event":"telegram_http","method":"%s","status":%d}', method, response.status_code)
            return None
        body = response.json()
        if not body.get("ok", False):
            self.errors += 1
            log.warning('{"event":"telegram_not_ok","method":"%s"}', method)
            return None
        return body.get("result")

    async def get_updates(self, *, timeout: int = 25) -> list[dict[str, Any]]:
        """One long-poll request. Returns [] on failure rather than raising."""
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "edited_message"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        result = await self._call("getUpdates", payload, timeout=timeout + 10)
        if not isinstance(result, list):
            return []
        # Acknowledge only what we were given; `serve` advances it after handling.
        return result

    def acknowledge(self, updates: list[dict[str, Any]]) -> None:
        """Move the offset past a batch that has been handled."""
        ids = [int(update["update_id"]) for update in updates if "update_id" in update]
        if ids:
            self.offset = max(ids) + 1

    async def send_message(self, chat_id: Any, text: str) -> bool:
        payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        result = await self._call("sendMessage", payload)
        if result is None:
            return False
        self.sent.append({"chat_id": chat_id, "text": text})
        return True


async def serve(
    router: Any,
    transport: TelegramTransport,
    *,
    poll_timeout: int = 25,
    max_backoff: float = 30.0,
    stop_after: int | None = None,
) -> dict[str, Any]:
    """Answer commands until cancelled. Returns a small health record.

    `stop_after` exists for tests and for a short supervised run; production passes None
    and relies on task cancellation.
    """
    from .commands import from_update

    answered = 0
    polls = 0
    backoff = 1.0
    while True:
        polls += 1
        try:
            updates = await transport.get_updates(timeout=poll_timeout)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - the listener must not die
            transport.errors += 1
            log.warning('{"event":"telegram_poll_failed","error":"%s"}', type(exc).__name__)
            await asyncio.sleep(min(max_backoff, backoff))
            backoff = min(max_backoff, backoff * 2.0)
            continue
        backoff = 1.0
        for update in updates:
            parsed = from_update(update)
            if parsed is None:
                continue
            chat_id, text = parsed
            reply = router.handle(chat_id, text)
            if reply.text:
                await transport.send_message(chat_id, reply.text)
            answered += 1
        if updates:
            transport.acknowledge(updates)
        if stop_after is not None and polls >= stop_after:
            break
    return {"polls": polls, "answered": answered, "offset": transport.offset, "errors": transport.errors}


def transport_from_config(config: Any, client: httpx.AsyncClient | None = None) -> TelegramTransport | None:
    """Build a transport if a token is configured, else None. Never raises on absence."""
    token = getattr(config.secrets, "telegram_bot_token", None)
    if not token:
        return None
    return TelegramTransport(token=token, client=client)
