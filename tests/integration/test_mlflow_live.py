"""Live tracking against a real MLflow tracking store (skipped when MLflow is absent).

The unit tests use a fake client, which proves the tracker's own logic but not that MLflow
accepts what it sends. This one points a real `MlflowClient` at a throwaway SQLite store -
the same backend a `mlflow server` uses - and reads the points back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradebot.ai.tracking import LiveTracker, read_server_runs

pytest.importorskip("mlflow")

NOW = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def store_uri(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'mlflow.db'}"


def test_points_written_live_are_read_back(store_uri: str) -> None:
    tracker = LiveTracker(store_uri, experiment="live-test", run_name="soak")
    run_id = tracker.start(params={"mode": "paper", "feed": "synthetic"})
    assert run_id, tracker.describe()

    for step in range(3):
        accepted = tracker.log(
            {"equity": 1000.0 + step, "trades_closed": step, "verdict": "NOT PROMOTABLE"},
            step=step,
            timestamp=NOW + timedelta(minutes=step),
        )
        assert accepted == 2  # the verdict is not a metric
    tracker.finish()

    rows = read_server_runs(store_uri, experiment="live-test")
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == run_id
    assert row["status"] == "FINISHED"
    assert row["metrics"]["equity"] == pytest.approx(1002.0)  # latest value wins
    assert row["metrics"]["trades_closed"] == pytest.approx(2)
    assert row["params"]["mode"] == "paper"
    assert row["tags"]["kind"] == "live_session"
    assert tracker.logged == 6


def test_a_dead_server_fails_fast_and_never_raises() -> None:
    """A URI nothing serves is a message, not a crashed or stalled session.

    MLflow's own HTTP client retries with backoff for minutes; the tracker's TCP pre-check
    is what keeps a dead tracking server from delaying the trading loop.
    """
    import time

    tracker = LiveTracker("http://127.0.0.1:1", experiment="dead")
    started = time.monotonic()
    assert tracker.start() is None
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"start() blocked for {elapsed:.1f}s on a dead tracker"
    assert tracker.disabled is True
    assert "cannot reach" in tracker.reason
    assert tracker.log({"equity": 1000}) == 0
    assert tracker.finish() is None
