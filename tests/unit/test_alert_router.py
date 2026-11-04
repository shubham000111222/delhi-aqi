"""Alert delivery: the floor, the second channel, and the queue (§20.3, §20.5).

The bug these tests exist to prevent is subtle: a channel list where the first success ends
the attempt looks correct and means the file channel silently replaces Telegram. The
distinction the router makes is `remote`, and it is what "independent alert channel" means.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tradebot.alerts.router import AlertRouter, FileChannel, QueuedAlert, WebhookChannel


class FakeRemote:
    """A remote channel with a scripted outcome."""

    remote = True

    def __init__(self, name: str, *, ok: bool = True, configured: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.configured = configured
        self.calls: list[str] = []

    async def send(self, text: str, *, level: str = "INFO") -> bool:
        self.calls.append(text)
        return self.ok


class ExplodingRemote(FakeRemote):
    async def send(self, text: str, *, level: str = "INFO") -> bool:
        self.calls.append(text)
        raise RuntimeError("transport is down")


class FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], str]] = []

    def log_event(self, kind: str, payload: dict[str, Any], *, level: str = "INFO", actor: str = "") -> int:
        self.events.append((kind, payload, level))
        return 1


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    return tmp_path / "alerts-queue.jsonl"


class TestTheFloor:
    async def test_the_file_is_written_whatever_the_remotes_say(self, tmp_path: Path):
        floor = FileChannel(path=tmp_path / "alerts.jsonl")
        broken = FakeRemote("telegram", ok=False)
        router = AlertRouter(channels=[floor, broken], queue_path=tmp_path / "q.jsonl")

        await router.send("the feed is stale", level="WARN")

        lines = (tmp_path / "alerts.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == "the feed is stale"

    async def test_a_successful_file_write_does_not_stop_the_remote_attempt(self, tmp_path: Path):
        floor = FileChannel(path=tmp_path / "alerts.jsonl")
        telegram = FakeRemote("telegram", ok=True)
        router = AlertRouter(channels=[floor, telegram], queue_path=tmp_path / "q.jsonl")

        delivered = await router.send("market closed", level="INFO")

        assert delivered is True
        assert telegram.calls == ["market closed"], "the floor must not swallow the alert"

    async def test_a_local_channel_that_raises_does_not_stop_the_alert(self, tmp_path: Path):
        class BrokenFloor(FileChannel):
            async def send(self, text: str, *, level: str = "INFO") -> bool:
                raise OSError("disk full")

        telegram = FakeRemote("telegram", ok=True)
        router = AlertRouter(
            channels=[BrokenFloor(path=tmp_path / "a.jsonl"), telegram], queue_path=tmp_path / "q.jsonl"
        )

        assert await router.send("still has to leave", level="CRIT") is True


class TestRemoteFailover:
    async def test_the_second_remote_is_tried_when_the_first_fails(self, tmp_path: Path):
        telegram = FakeRemote("telegram", ok=False)
        webhook = FakeRemote("webhook", ok=True)
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), telegram, webhook],
            queue_path=tmp_path / "q.jsonl",
        )

        assert await router.send("bot down", level="CRIT") is True
        assert webhook.calls == ["bot down"]
        assert router.pending() == [], "a delivered alert must not also be queued"

    async def test_a_raising_transport_counts_as_a_failure_and_the_next_one_runs(self, tmp_path: Path):
        exploding = ExplodingRemote("telegram")
        webhook = FakeRemote("webhook", ok=True)
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), exploding, webhook],
            queue_path=tmp_path / "q.jsonl",
        )

        assert await router.send("watchdog fired", level="CRIT") is True
        assert router.failed_channels["telegram"] == 1

    async def test_unconfigured_channels_are_not_counted_as_failures(self, tmp_path: Path):
        telegram = FakeRemote("telegram", ok=False, configured=False)
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), telegram], queue_path=tmp_path / "q.jsonl"
        )

        await router.send("anything", level="WARN")

        assert router.failed_channels == {}
        assert router.independent is False

    async def test_independence_needs_two_configured_remote_channels(self, tmp_path: Path):
        floor = FileChannel(path=tmp_path / "a.jsonl")
        telegram = FakeRemote("telegram", configured=True)
        webhook = FakeRemote("webhook", configured=False)
        router = AlertRouter(channels=[floor, telegram, webhook], queue_path=tmp_path / "q.jsonl")

        assert router.independent is False
        webhook.configured = True
        assert router.independent is True


class TestTheQueue:
    async def test_an_alert_no_remote_took_is_written_to_disk(self, tmp_path: Path):
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), FakeRemote("telegram", ok=False)],
            queue_path=tmp_path / "q.jsonl",
        )

        delivered = await router.send("nobody heard this", level="CRIT")

        assert delivered is False
        pending = router.pending()
        assert len(pending) == 1
        assert pending[0].text == "nobody heard this"
        assert pending[0].attempts == 0

    async def test_flush_delivers_and_empties_the_queue(self, tmp_path: Path, queue: Path):
        telegram = FakeRemote("telegram", ok=False)
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), telegram], queue_path=queue
        )
        await router.send("queued once", level="CRIT")
        assert router.pending()

        telegram.ok = True  # the transport comes back
        delivered = await router.flush()

        assert delivered == 1
        assert router.pending() == []

    async def test_a_failed_flush_keeps_the_alert_and_counts_the_attempt(self, tmp_path: Path, queue: Path):
        telegram = FakeRemote("telegram", ok=False)
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), telegram], queue_path=queue
        )
        await router.send("still stuck", level="CRIT")

        assert await router.flush() == 0
        pending = router.pending()
        assert len(pending) == 1
        assert pending[0].attempts == 1

    async def test_it_stops_retrying_after_the_attempt_ceiling(self, tmp_path: Path, queue: Path):
        telegram = FakeRemote("telegram", ok=False)
        store = FakeStore()
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), telegram],
            queue_path=queue,
            store=store,
            max_attempts=2,
        )
        await router.send("hopeless", level="CRIT")
        for _ in range(3):
            await router.flush()

        assert router.pending() == [], "an exhausted alert must not sit in the queue forever"
        assert any(kind == "alert_expired" for kind, _, _ in store.events)

    async def test_an_alert_past_its_age_ceiling_is_dropped(self, tmp_path: Path, queue: Path):
        store = FakeStore()
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), FakeRemote("telegram", ok=False)],
            queue_path=queue,
            store=store,
            max_age=timedelta(seconds=0),
        )
        await router.send("too old to matter", level="WARN")
        await router.flush()

        assert any(kind == "alert_expired" for kind, _, _ in store.events)

    async def test_a_corrupt_queue_line_is_ignored_not_fatal(self, tmp_path: Path, queue: Path):
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text('{"ts": "not-a-date"\nnot json at all\n', encoding="utf-8")
        router = AlertRouter(
            channels=[FileChannel(path=tmp_path / "a.jsonl"), FakeRemote("telegram", ok=True)],
            queue_path=queue,
        )

        assert router.pending() == [] or all(isinstance(a, QueuedAlert) for a in router.pending())
        assert await router.flush() == 0

    async def test_the_router_reports_what_it_did(self, tmp_path: Path):
        store = FakeStore()
        router = AlertRouter(
            channels=[
                FileChannel(path=tmp_path / "a.jsonl"),
                FakeRemote("telegram", ok=False),
                FakeRemote("webhook", ok=False),
            ],
            queue_path=tmp_path / "q.jsonl",
            store=store,
        )
        await router.send("two failures", level="CRIT")

        summary = router.as_dict()
        assert summary["delivered"] == 0
        assert summary["queued"] == 1
        assert summary["pending"] == 1
        assert summary["failed_channels"] == {"telegram": 1, "webhook": 1}
        assert [kind for kind, _, _ in store.events] == ["alert_delivery"]


class TestWebhookChannel:
    async def test_it_posts_json_and_treats_any_2xx_or_3xx_as_delivered(self):
        class Response:
            status_code = 204

        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def post(self, url: str, **kwargs: Any) -> Response:
                self.calls.append((url, kwargs["json"]))
                return Response()

        client = Client()
        channel = WebhookChannel("https://example.invalid/hook", client=client)  # type: ignore[arg-type]

        assert await channel.send("hello", level="WARN") is True
        url, payload = client.calls[0]
        assert url == "https://example.invalid/hook"
        assert payload == {"text": "hello", "level": "WARN"}

    async def test_an_unconfigured_webhook_sends_nothing(self):
        channel = WebhookChannel("")
        assert channel.configured is False
        assert await channel.send("nobody home") is False
