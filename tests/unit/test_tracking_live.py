"""Live MLflow tracking: what goes on the wire, and what happens when the wire is dead."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from tradebot.ai.tracking import LiveTracker

NOW = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
SQLITE = "sqlite:///:memory:"  # a store the TCP pre-check leaves alone


class FakeClient:
    """Records every call the tracker makes, in order."""

    def __init__(self, *, fail_on: str | None = None, experiments: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_on = fail_on
        self.experiments = dict(experiments or {})
        self.created: list[dict[str, Any]] = []

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise RuntimeError(f"tracking server refused {name}")

    def get_experiment_by_name(self, name: str) -> Any:
        self._maybe_fail("get_experiment_by_name")
        self.calls.append(("get_experiment_by_name", (name,), {}))
        found = self.experiments.get(name)
        return SimpleNamespace(experiment_id=found) if found else None

    def create_experiment(self, name: str) -> str:
        self._maybe_fail("create_experiment")
        self.calls.append(("create_experiment", (name,), {}))
        self.experiments[name] = "7"
        return "7"

    def create_run(self, experiment_id: str, run_name: str | None = None, tags: dict[str, Any] | None = None) -> Any:
        self._maybe_fail("create_run")
        self.calls.append(("create_run", (experiment_id,), {"run_name": run_name, "tags": tags}))
        self.created.append({"experiment_id": experiment_id, "run_name": run_name, "tags": tags})
        return SimpleNamespace(info=SimpleNamespace(run_id="run-abc"))

    def log_param(self, run_id: str, key: str, value: str) -> None:
        self._maybe_fail("log_param")
        self.calls.append(("log_param", (run_id, key, value), {}))

    def log_metric(self, run_id: str, key: str, value: float, timestamp: int, step: int) -> None:
        self._maybe_fail("log_metric")
        self.calls.append(("log_metric", (run_id, key, value), {"timestamp": timestamp, "step": step}))

    def set_terminated(self, run_id: str, status: str = "FINISHED") -> None:
        self._maybe_fail("set_terminated")
        self.calls.append(("set_terminated", (run_id, status), {}))

    def metrics(self) -> dict[str, float]:
        return {args[1]: args[2] for name, args, _ in self.calls if name == "log_metric"}


class TestLiveTracker:
    def test_start_creates_the_experiment_and_the_run(self) -> None:
        client = FakeClient()
        tracker = LiveTracker(SQLITE, client=client, run_name="soak-1")

        run_id = tracker.start(params={"mode": "paper"}, tags={"feed": "synthetic"})

        assert run_id == "run-abc"
        assert tracker.experiment_id == "7"
        assert client.created[0]["experiment_id"] == "7"
        assert client.created[0]["tags"]["kind"] == "live_session"
        assert client.created[0]["tags"]["feed"] == "synthetic"
        params = {args[1]: args[2] for name, args, _ in client.calls if name == "log_param"}
        assert params == {"mode": "paper"}

    def test_existing_experiment_is_reused(self) -> None:
        client = FakeClient(experiments={"tradebot-live": "3"})
        tracker = LiveTracker(SQLITE, client=client)

        tracker.start()

        assert tracker.experiment_id == "3"
        assert not [call for call in client.calls if call[0] == "create_experiment"]

    def test_only_numeric_metrics_reach_the_server(self) -> None:
        client = FakeClient()
        tracker = LiveTracker(SQLITE, client=client)
        tracker.start()

        accepted = tracker.log(
            {"equity": "999.5", "verdict": "NOT PROMOTABLE", "missing": None, "kill_level": 1},
            step=2,
            timestamp=NOW,
        )

        assert accepted == 2
        assert client.metrics() == {"equity": 999.5, "kill_level": 1}
        assert tracker.skipped == 2
        steps = {call[2]["step"] for call in client.calls if call[0] == "log_metric"}
        assert steps == {2}

    def test_log_before_start_is_a_no_op(self) -> None:
        tracker = LiveTracker(SQLITE, client=FakeClient())

        assert tracker.log({"equity": 1000}) == 0
        assert tracker.logged == 0

    def test_a_dead_server_disables_the_tracker_instead_of_the_session(self) -> None:
        client = FakeClient(fail_on="log_metric")
        tracker = LiveTracker(SQLITE, client=client)
        tracker.start()

        first = tracker.log({"equity": 1000})
        second = tracker.log({"equity": 1001})

        assert first == 0
        assert second == 0
        assert tracker.disabled is True
        assert "log_metric" in tracker.last_error
        assert tracker.failures == 1  # not one per interval: the tracker stops trying
        assert tracker.describe()["enabled"] is False

    def test_a_dead_server_at_start_reports_why(self) -> None:
        tracker = LiveTracker(SQLITE, client=FakeClient(fail_on="create_run"))

        assert tracker.start() is None
        assert tracker.disabled is True
        assert tracker.describe()["reason"] == "" or tracker.last_error

    def test_finish_terminates_the_run(self) -> None:
        client = FakeClient()
        tracker = LiveTracker(SQLITE, client=client)
        tracker.start()

        tracker.finish()

        terminated = [call for call in client.calls if call[0] == "set_terminated"]
        assert terminated == [("set_terminated", ("run-abc", "FINISHED"), {})]

    def test_describe_reports_the_counters(self) -> None:
        client = FakeClient()
        tracker = LiveTracker(SQLITE, client=client, experiment="soak")
        tracker.start()
        tracker.log({"equity": 1000, "note": "text"})

        described = tracker.describe()

        assert described["tracking_uri"] == SQLITE
        assert described["experiment"] == "soak"
        assert described["enabled"] is True
        assert described["points"] == 1
        assert described["skipped"] == 1


class TestReadServerRuns:
    def test_rows_are_normalised_to_the_file_store_shape(self) -> None:
        from tradebot.ai.tracking import read_server_runs

        run = SimpleNamespace(
            info=SimpleNamespace(run_id="r1", experiment_id="7", status="FINISHED", start_time=1700000000000),
            data=SimpleNamespace(
                metrics={"equity": "1001.5"},
                params={"mode": "paper"},
                tags={"kind": "live_session"},
            ),
        )

        class Client:
            def search_runs(self, **kwargs: Any) -> list[Any]:
                assert kwargs["max_results"] == 5
                assert kwargs["experiment_ids"] == ["7", "8"]
                return [run]

            def search_experiments(self) -> list[Any]:
                return [SimpleNamespace(experiment_id="7"), SimpleNamespace(experiment_id="8")]

        rows = read_server_runs("http://x", limit=5, client=Client())

        assert rows == [
            {
                "run_id": "r1",
                "experiment": "7",
                "status": "FINISHED",
                "start_time": 1700000000000,
                "metrics": {"equity": 1001.5},
                "params": {"mode": "paper"},
                "tags": {"kind": "live_session"},
            }
        ]

    def test_missing_mlflow_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host without MLflow keeps trading; the tracker explains why it is off."""
        import builtins

        tracker = LiveTracker(SQLITE)
        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("mlflow"):
                raise ImportError("no mlflow here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert tracker.start() is None
        assert tracker.disabled is True
        assert "mlflow" in tracker.reason


class TestReachability:
    def test_an_http_server_that_nothing_answers_is_refused_quickly(self) -> None:
        """The pre-check exists so a dead tracker cannot stall the session start."""
        tracker = LiveTracker("http://127.0.0.1:1", client=FakeClient())

        assert tracker.start() is None
        assert tracker.disabled is True
        assert "cannot reach" in tracker.reason

    def test_a_non_http_store_is_not_probed(self) -> None:
        tracker = LiveTracker(SQLITE, client=FakeClient())

        assert tracker._reachable() == (True, "")
