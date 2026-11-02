"""Alert delivery: Telegram when configured, always to the log and the event store.

Deduplicated and rate-limited (section 12): one alert per (key) per window, so a
looping failure cannot turn into a message storm.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Config
from ..core.logging import get_logger

log = get_logger(__name__)

LEVELS = {"INFO": 10, "WARN": 20, "CRIT": 30}


@dataclass
class SentAlert:
    ts: float
    level: str
    text: str


class Notifier:
    def __init__(self, config: Config, store: Any = None, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = config
        self.store = store
        self.token = config.secrets.telegram_bot_token
        self.chat_id = config.secrets.telegram_chat_id
        self.min_level = config.settings.alerts.min_level.upper()
        self.dedupe_seconds = config.settings.alerts.dedupe_seconds
        self._recent: dict[str, float] = {}
        self._client = client
        self.sent: list[SentAlert] = []

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _should_send(self, level: str, key: str) -> bool:
        if LEVELS.get(level, 0) < LEVELS.get(self.min_level, 20):
            return False
        last = self._recent.get(key)
        now = time.monotonic()
        if last is not None and now - last < self.dedupe_seconds:
            return False
        self._recent[key] = now
        return True

    async def send(self, text: str, *, level: str = "INFO", key: str | None = None) -> bool:
        key = key or text[:64]
        if self.store is not None:
            self.store.log_event(
                "alert", {"level": level, "text": text[:2000]}, level=level, actor="alerts"
            )
        if not self._should_send(level, key):
            return False
        log.info('{"event":"alert","level":"%s","text":"%s"}', level, text.replace("\n", " | ")[:400])
        self.sent.append(SentAlert(time.monotonic(), level, text))
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, timeout=10)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json=payload, timeout=10)
            return response.status_code == 200
        except Exception as exc:  # noqa: BLE001 - alerts must never break trading
            log.warning('{"event":"telegram_failed","error":"%s"}', exc)
            if self.store is not None:
                self.store.log_event("alert_failed", {"error": str(exc)}, level="WARN", actor="alerts")
            return False

    async def critical(self, text: str, key: str | None = None) -> bool:
        return await self.send(text, level="CRIT", key=key)

    async def warn(self, text: str, key: str | None = None) -> bool:
        return await self.send(text, level="WARN", key=key)


class ConsoleNotifier(Notifier):
    """Used by tests and CLI runs without Telegram credentials."""

    async def send(self, text: str, *, level: str = "INFO", key: str | None = None) -> bool:
        await super().send(text, level=level, key=key)
        print(f"[{level}] {text}")
        return True
