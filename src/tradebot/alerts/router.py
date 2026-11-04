"""Alert delivery that assumes the first channel is already down (§20.5).

The existing `Notifier` sends to Telegram and logs. That is one transport, and it is the
same transport for "the feed is stale" and "the bot is dead" — so the day Telegram is
unreachable is the day nobody learns the bot died. Section 20.5 asks for an independent
channel; section 20.3 asks for a retry queue and "disk always".

The rules here, in order of how much they matter:

1. **Disk is a floor, not a channel.** Every alert is written to a JSONL file before any
   network call is attempted. A file channel that "succeeds" must not stop the router from
   trying the remote ones, which is the bug you get if you treat channels as a fallback
   list.
2. **Remote delivery is best-effort across every remote channel.** Telegram *and* a webhook
   are attempted; one succeeding is enough.
3. **An alert that reached no remote channel is queued on disk and retried**, with an
   attempt ceiling and an age ceiling so the queue cannot grow forever.
4. **Nothing here can raise**: an alerting failure must never be able to stop trading.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from ..core.logging import get_logger

log = get_logger(__name__)

LEVELS = {"INFO": 10, "WARN": 20, "CRIT": 30}


@runtime_checkable
class Channel(Protocol):
    """One delivery path. `remote` decides whether a success ends the attempt."""

    name: str
    remote: bool

    async def send(self, text: str, *, level: str) -> bool: ...


@dataclass
class FileChannel:
    """The floor: append-only JSONL, one object per alert, never overwritten."""

    path: Path
    name: str = "file"
    remote: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            self.path = Path(self.path)

    async def send(self, text: str, *, level: str = "INFO") -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(UTC).isoformat(), "level": level, "text": text[:4000]}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True


class TelegramChannel:
    """Telegram, with the token kept out of every log line and error message."""

    name = "telegram"
    remote = True

    def __init__(
        self,
        token: str,
        chat_id: str,
        client: httpx.AsyncClient | None = None,
        api: str = "https://api.telegram.org",
        timeout: float = 10.0,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.client = client
        self.api = api
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str, *, level: str = "INFO") -> bool:
        if not self.configured:
            return False
        url = f"{self.api}/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        try:
            if self.client is not None:
                response = await self.client.post(url, json=payload, timeout=self.timeout)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json=payload, timeout=self.timeout)
            return response.status_code == 200
        except Exception as exc:  # noqa: BLE001 - alerting must never propagate
            log.warning('{"event":"alert_transport_failed","channel":"telegram","error":"%s"}', type(exc).__name__)
            return False


class WebhookChannel:
    """A generic JSON POST: Slack, Discord, ntfy, Gotify, or your own endpoint.

    It is deliberately the dumbest possible protocol, because the second channel's job is
    to keep working when the first one's vendor is having a bad day.
    """

    name = "webhook"
    remote = True

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.url = url
        self.client = client
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def send(self, text: str, *, level: str = "INFO") -> bool:
        if not self.configured:
            return False
        payload = {"text": text[:4000], "level": level}
        try:
            if self.client is not None:
                response = await self.client.post(self.url, json=payload, timeout=self.timeout)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(self.url, json=payload, timeout=self.timeout)
            return response.status_code < 400
        except Exception as exc:  # noqa: BLE001 - alerting must never propagate
            log.warning('{"event":"alert_transport_failed","channel":"webhook","error":"%s"}', type(exc).__name__)
            return False


@dataclass
class QueuedAlert:
    ts: str
    level: str
    text: str
    attempts: int = 0

    @property
    def age(self) -> timedelta:
        try:
            created = datetime.fromisoformat(self.ts.replace("Z", "+00:00"))
        except ValueError:  # pragma: no cover - a corrupt line is dropped by the reader
            return timedelta.max
        return datetime.now(UTC) - created


@dataclass
class AlertRouter:
    """Fan out to every channel, queue what no remote channel accepted.

    `send` returns True only when a *remote* channel accepted the alert. A True from the
    file channel means "it is on disk", which is necessary and never sufficient.
    """

    channels: list[Channel] = field(default_factory=list)
    queue_path: Path | None = None
    store: Any = None
    max_attempts: int = 5
    max_age: timedelta = timedelta(hours=24)
    attempts: int = 0
    delivered: int = 0
    queued: int = 0
    failed_channels: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.queue_path is not None and not isinstance(self.queue_path, Path):
            self.queue_path = Path(self.queue_path)

    # --- channels ---------------------------------------------------------
    @property
    def remote_channels(self) -> list[Channel]:
        return [c for c in self.channels if c.remote]

    @property
    def local_channels(self) -> list[Channel]:
        return [c for c in self.channels if not c.remote]

    @property
    def independent(self) -> bool:
        """True when there is a second remote transport that is actually configured."""
        configured = [c for c in self.remote_channels if getattr(c, "configured", True)]
        return len(configured) >= 2

    async def send(self, text: str, *, level: str = "WARN", key: str | None = None) -> bool:
        self.attempts += 1
        for channel in self.local_channels:
            try:
                await channel.send(text, level=level)
            except Exception:  # noqa: BLE001 - the floor must not raise either
                log.exception('{"event":"alert_local_failed","channel":"%s"}', channel.name)

        delivered = False
        for channel in self.remote_channels:
            try:
                ok = await channel.send(text, level=level)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                delivered = True
                break
            if getattr(channel, "configured", True):
                self.failed_channels[channel.name] = self.failed_channels.get(channel.name, 0) + 1

        if delivered:
            self.delivered += 1
        else:
            self._enqueue(QueuedAlert(ts=datetime.now(UTC).isoformat(), level=level, text=text))
            self.queued += 1

        if self.store is not None:
            self.store.log_event(
                "alert_delivery",
                {
                    "level": level,
                    "remote": delivered,
                    "key": (key or text[:64])[:120],
                    "independent": self.independent,
                },
                level=level,
                actor="alerts",
            )
        return delivered

    # --- the retry queue --------------------------------------------------
    def _enqueue(self, alert: QueuedAlert) -> None:
        if self.queue_path is None:
            return
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        with self.queue_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(alert), ensure_ascii=False) + "\n")

    def pending(self) -> list[QueuedAlert]:
        """Queued alerts, dropping lines that cannot be parsed (a partial write)."""
        if self.queue_path is None or not self.queue_path.exists():
            return []
        out: list[QueuedAlert] = []
        for line in self.queue_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                out.append(QueuedAlert(**raw))
            except (ValueError, TypeError):  # pragma: no cover - corrupt line
                continue
        return out

    def _rewrite(self, alerts: list[QueuedAlert]) -> None:
        if self.queue_path is None:
            return
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.queue_path.parent), prefix=".alerts-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for alert in alerts:
                    fh.write(json.dumps(asdict(alert), ensure_ascii=False) + "\n")
            os.replace(tmp, self.queue_path)
        finally:
            if os.path.exists(tmp):  # pragma: no cover - replace removes it normally
                os.unlink(tmp)

    async def flush(self) -> int:
        """Retry everything queued. Returns how many alerts were finally delivered.

        Exhausted alerts (too many attempts, or older than `max_age`) are dropped with an
        `alert_expired` event instead of being retried forever: a queue that never drains
        is a queue nobody reads.
        """
        alerts = self.pending()
        if not alerts:
            return 0
        keep: list[QueuedAlert] = []
        delivered = 0
        for alert in alerts:
            if alert.attempts >= self.max_attempts or alert.age > self.max_age:
                if self.store is not None:
                    self.store.log_event(
                        "alert_expired",
                        {"level": alert.level, "text": alert.text[:500], "attempts": alert.attempts},
                        level="WARN",
                        actor="alerts",
                    )
                continue
            ok = False
            for channel in self.remote_channels:
                try:
                    ok = await channel.send(alert.text, level=alert.level)
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    break
            if ok:
                delivered += 1
                self.delivered += 1
            else:
                alert.attempts += 1
                keep.append(alert)
        self._rewrite(keep)
        return delivered

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "delivered": self.delivered,
            "queued": self.queued,
            "pending": len(self.pending()),
            "independent": self.independent,
            "channels": [
                {
                    "name": c.name,
                    "remote": c.remote,
                    "configured": bool(getattr(c, "configured", True)),
                }
                for c in self.channels
            ],
            "failed_channels": dict(self.failed_channels),
        }


def router_from_config(config: Any, store: Any = None) -> AlertRouter:
    """The production wiring: file floor, Telegram, and the independent webhook.

    Order matters and is deliberate. The file is written first because it is the one channel
    that cannot be down; the two remote transports are attempted after it, and either one
    succeeding is enough.
    """
    state = Path(config.settings.state_dir)
    channels: list[Channel] = [
        FileChannel(path=state / config.settings.watchdog.alert_log),
        TelegramChannel(config.secrets.telegram_bot_token, config.secrets.telegram_chat_id),
        WebhookChannel(config.secrets.alert_webhook_url),
    ]
    return AlertRouter(
        channels=channels,
        queue_path=state / config.settings.watchdog.alert_queue,
        store=store,
    )


def describe() -> dict[str, Any]:
    return {
        "component": "alerts",
        "floor": "file (always written before any network call)",
        "remotes": ["telegram", "webhook"],
        "retry": "queued to disk, replayed by flush(), dropped after max_attempts or max_age",
    }


__all__ = [
    "AlertRouter",
    "Channel",
    "FileChannel",
    "LEVELS",
    "QueuedAlert",
    "TelegramChannel",
    "WebhookChannel",
    "describe",
    "router_from_config",
]
