"""MLflow file-store export: the layout, the numbers, and what happens to a verdict."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tradebot.ai.tracking import export_runs, read_runs, session_run, write_experiment
from tradebot.core.store import Store

NOW = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def _has_mlflow() -> bool:
    try:
        import mlflow  # noqa: F401
    except ImportError:
        return False
    return True


def run(run_id: str = "run-1", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "created_at": "2024-01-01T09:30:00+00:00",
        "kind": "backtest",
        "strategy": "S1_ema_trend",
        "params": {"ema_fast": 12, "ema_slow": 26, "split": "2023-Q4"},
        "metrics": {"sharpe": 1.42, "profit_factor": 1.51, "max_drawdown_pct": -8.2},
        "git_commit": "abc123",
        "data_hash": "sha256:deadbeef",
        "seed": 7,
        "notes": "walk-forward window 1",
    }
    payload.update(overrides)
    return payload


class TestLayout:
    def test_a_run_lands_where_mlflow_looks_for_it(self, tmp_path: Path):
        report = export_runs([run()], tmp_path, now=NOW)
        experiment = tmp_path / "0"
        meta = (experiment / "meta.yaml").read_text()
        assert "experiment_id: 0" in meta
        assert 'name: "tradebot"' in meta

        run_dir = experiment / "run-1"
        assert (run_dir / "meta.yaml").exists()
        assert (run_dir / "metrics" / "sharpe").exists()
        assert (run_dir / "params" / "ema_fast").read_text() == "12"
        assert (run_dir / "tags" / "git_commit").read_text() == "abc123"
        assert report.runs == 1
        assert report.metrics == 3
        assert "MLFLOW_ALLOW_FILE_STORE=true" in report.as_dict()["ui"]

    def test_a_metric_line_is_time_value_step(self, tmp_path: Path):
        export_runs([run()], tmp_path, now=NOW)
        line = (tmp_path / "0" / "run-1" / "metrics" / "sharpe").read_text().strip()
        stamp, value, step = line.split()
        assert int(stamp) == 1_704_101_400_000  # 2024-01-01T09:30Z in ms
        assert float(value) == 1.42
        assert step == "0"

    def test_the_experiment_directory_is_reused_on_a_second_export(self, tmp_path: Path):
        export_runs([run()], tmp_path, now=NOW)
        export_runs([run("run-2")], tmp_path, now=NOW)
        assert (tmp_path / "0" / "run-1").exists()
        assert (tmp_path / "0" / "run-2").exists()
        assert len(list((tmp_path / "0").glob("*/meta.yaml"))) == 2

    def test_exporting_the_same_run_twice_overwrites_it(self, tmp_path: Path):
        export_runs([run()], tmp_path, now=NOW)
        export_runs([run(metrics={"sharpe": 2.0})], tmp_path, now=NOW)
        assert (tmp_path / "0" / "run-1" / "metrics" / "sharpe").read_text().strip().endswith("2.0 0")

    def test_an_awkward_run_id_becomes_a_boring_directory_name(self, tmp_path: Path):
        export_runs([run("grid/S1 12-20 #3")], tmp_path, now=NOW)
        names = [p.name for p in (tmp_path / "0").iterdir() if p.is_dir()]
        assert names == ["grid_S1_12-20__3"]

    def test_write_experiment_can_be_called_on_its_own(self, tmp_path: Path):
        path = write_experiment(tmp_path, name="slippage", experiment_id="3")
        assert (path / "meta.yaml").read_text().splitlines()[0] == "experiment_id: 3"


class TestHonesty:
    def test_a_string_metric_becomes_a_tag_instead_of_breaking_mlflow(self, tmp_path: Path):
        report = export_runs(
            [run(metrics={"sharpe": 1.4, "verdict": "NOT PROMOTABLE"})], tmp_path, now=NOW
        )
        run_dir = tmp_path / "0" / "run-1"
        assert not (run_dir / "metrics" / "verdict").exists()
        assert (run_dir / "tags" / "metric_verdict").read_text() == "NOT PROMOTABLE"
        assert report.runs_as_tags == 1

    def test_a_numeric_string_is_still_a_metric(self, tmp_path: Path):
        export_runs([run(metrics={"total_return": "0.031"})], tmp_path, now=NOW)
        assert (tmp_path / "0" / "run-1" / "metrics" / "total_return").exists()

    def test_a_run_without_an_id_is_skipped_and_said_so(self, tmp_path: Path):
        report = export_runs([{"run_id": "", "metrics": {}}], tmp_path, now=NOW)
        assert report.runs == 0
        assert report.skipped == ["(missing run_id)"]

    def test_empty_tags_are_not_written(self, tmp_path: Path):
        export_runs([run(git_commit="")], tmp_path, now=NOW)
        assert not (tmp_path / "0" / "run-1" / "tags" / "git_commit").exists()

    def test_a_nested_param_is_serialised_not_crashed(self, tmp_path: Path):
        export_runs([run(params={"grid": {"ema_fast": [12, 20]}})], tmp_path, now=NOW)
        text = (tmp_path / "0" / "run-1" / "params" / "grid").read_text()
        assert "ema_fast" in text


class TestFromTheStore:
    def test_experiments_recorded_by_the_bot_can_be_exported(self, tmp_path: Path):
        store = Store(":memory:")
        try:
            store.save_experiment(
                "run-a",
                kind="backtest",
                strategy="S1_ema_trend",
                params={"ema_fast": 12},
                metrics={"sharpe": 0.9, "verdict": "reject"},
                git_commit="abc123",
                seed=1,
            )
            store.save_experiment("run-b", kind="sensitivity", strategy="S2_meanrev", metrics={"score": -0.01})
            report = export_runs(store.experiments(), tmp_path, now=NOW)
        finally:
            store.close()
        assert report.runs == 2
        assert (tmp_path / "0" / "run-a" / "tags" / "strategy").read_text() == "S1_ema_trend"
        assert (tmp_path / "0" / "run-b" / "metrics" / "score").exists()
        assert report.runs_as_tags >= 1, "the 'reject' verdict must not be forced into a metric"


class TestAgainstRealMlflow:
    """If MLflow is installed, it is the judge of whether this is an MLflow store."""

    def test_mlflow_reads_a_run_we_wrote(self, tmp_path: Path, monkeypatch) -> None:
        if not _has_mlflow():
            pytest.skip("mlflow is not installed; the layout is verified by the tests above")
        monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
        export_runs([run()], tmp_path, now=NOW)

        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=f"file://{tmp_path}")
        experiments = [e.name for e in client.search_experiments()]
        assert "tradebot" in experiments

        found = client.search_runs(experiment_ids=["0"])
        assert len(found) == 1
        entry = found[0]
        assert entry.data.metrics["sharpe"] == 1.42
        assert entry.data.params["ema_fast"] == "12"
        assert entry.data.tags["git_commit"] == "abc123"
        assert "verdict" not in entry.data.metrics


class TestReadingBack:
    def test_what_we_export_is_what_we_read(self, tmp_path: Path):
        export_runs([run()], tmp_path, now=NOW)
        runs = read_runs(tmp_path)
        assert len(runs) == 1
        back = runs[0]
        assert back["run_id"] == "run-1"
        assert back["experiment"] == "tradebot"
        assert back["metrics"]["sharpe"] == pytest.approx(1.42)
        assert back["params"]["ema_fast"] == "12"
        assert back["tags"]["git_commit"] == "abc123"

    def test_a_store_that_is_not_there_reads_as_nothing(self, tmp_path: Path):
        assert read_runs(tmp_path / "missing") == []

    def test_a_half_written_store_is_skipped_not_fatal(self, tmp_path: Path):
        export_runs([run()], tmp_path, now=NOW)
        broken = tmp_path / "0" / "run-broken"
        (broken / "metrics").mkdir(parents=True)
        (broken / "meta.yaml").write_text("run_id: run-broken\n")
        (broken / "metrics" / "sharpe").write_text("not-a-number\n")
        runs = read_runs(tmp_path)
        assert [r["run_id"] for r in runs] == ["run-1", "run-broken"]
        assert runs[1]["metrics"] == {}

    def test_the_limit_is_respected(self, tmp_path: Path):
        export_runs([run(f"run-{i}") for i in range(5)], tmp_path, now=NOW)
        assert len(read_runs(tmp_path, limit=2)) == 2


class TestSessionRun:
    def test_a_session_becomes_one_comparable_run(self, tmp_path: Path):
        status = {
            "equity": "1012.5",
            "cash": "900",
            "realized_pnl": "12.5",
            "unrealized_pnl": "0",
            "fees": "3.25",
            "kill_state": {"level": "L1"},
            "stats": {"signals": 40, "vetoes": 30, "orders": 10, "fills": 10, "trades_closed": 4},
        }
        session = session_run(
            run_id="session-1", store=Store(":memory:"), status=status, started=NOW,
            mode="paper", capital=250, extra_tags={"news_feeds": "https://example/rss"},
        )
        assert session["metrics"]["equity"] == "1012.5"
        assert session["metrics"]["trades_closed"] == 4
        assert session["params"]["capital"] == 250
        assert session["tags"]["kill_level"] == "L1"
        assert session["tags"]["news_feeds"] == "https://example/rss"

        export_runs([session], tmp_path, now=NOW)
        back = read_runs(tmp_path)[0]
        assert back["metrics"]["equity"] == pytest.approx(1012.5)
        assert back["params"]["mode"] == "paper"
        assert back["tags"]["kill_level"] == "L1"

    def test_no_closed_trades_reports_a_zero_win_rate_not_a_crash(self):
        session = session_run(
            run_id="s", store=Store(":memory:"), status={"stats": {"trades_closed": 0}},
            started=NOW,
        )
        assert session["metrics"]["win_rate"] == 0.0
