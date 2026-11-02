"""MLflow experiment export (plan §18.6).

The repository has kept every backtest, grid cell and signal screen in an `experiments`
table since the multiple-testing work, but that table only exists inside SQLite. MLflow is
the standard place to compare runs, and importing it as a dependency for a demo bot is
heavy - so this module writes the **file store layout MLflow itself reads**:

    mlruns/<experiment_id>/meta.yaml
    mlruns/<experiment_id>/<run_id>/meta.yaml
    mlruns/<experiment_id>/<run_id>/metrics/<key>     # "<ms> <value> <step>"
    mlruns/<experiment_id>/<run_id>/params/<key>
    mlruns/<experiment_id>/<run_id>/tags/<key>

`mlflow ui --backend-store-uri file:///abs/path/mlruns` then shows the runs, with no
`mlflow` import in the bot and nothing to install to keep trading.

Two rules that matter for correctness:

* **Metrics must be numeric.** MLflow refuses a metric that is a string; a verdict like
  "NOT PROMOTABLE" belongs in a tag. Non-numeric values are routed to tags, and the
  report counts how many were.
* **Timestamps are milliseconds and ascending.** MLflow reads a metric file as a history;
  writing one line at time zero is fine, writing them out of order is not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.clock import utc, utcnow

SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def _safe(key: str) -> str:
    """MLflow keys become filenames: keep them boring."""
    cleaned = SAFE.sub("_", str(key)).strip("._")
    return cleaned or "unnamed"


def _millis(value: Any, fallback: datetime) -> int:
    if isinstance(value, datetime):
        return int(utc(value).timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    if isinstance(value, str):
        try:
            return int(utc(datetime.fromisoformat(value.replace("Z", "+00:00"))).timestamp() * 1000)
        except ValueError:
            pass
    return int(utc(fallback).timestamp() * 1000)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


@dataclass
class ExportReport:
    root: str
    experiment_id: str
    experiment_name: str
    runs: int = 0
    metrics: int = 0
    params: int = 0
    tags: int = 0
    runs_as_tags: int = 0
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "runs": self.runs,
            "metrics": self.metrics,
            "params": self.params,
            "tags": self.tags,
            "non_numeric_metrics_moved_to_tags": self.runs_as_tags,
            "skipped": self.skipped,
            # MLflow 3 keeps the file store behind an opt-out; without this env var the
            # UI refuses to open a directory store at all.
            "ui": (
                "MLFLOW_ALLOW_FILE_STORE=true mlflow ui "
                f"--backend-store-uri file://{Path(self.root).resolve()}"
            ),
        }


def write_experiment(root: str | Path, name: str = "tradebot", experiment_id: str = "0") -> Path:
    """Create/refresh the experiment directory. Returns the experiment path."""
    directory = Path(root) / str(experiment_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = directory / "meta.yaml"
    if not meta.exists():
        meta.write_text(
            f"experiment_id: {experiment_id}\n"
            f'name: "{name}"\n'
            f"artifact_location: file://{directory.resolve()}/artifacts\n"
            "lifecycle_stage: active\n"
        )
    return directory


def export_runs(
    runs: list[dict[str, Any]],
    root: str | Path,
    *,
    name: str = "tradebot",
    experiment_id: str = "0",
    now: datetime | None = None,
) -> ExportReport:
    """Write runs in MLflow's file-store layout. Safe to run twice: it overwrites."""
    experiment = write_experiment(root, name=name, experiment_id=experiment_id)
    report = ExportReport(root=str(root), experiment_id=experiment_id, experiment_name=name)
    moment = now or utcnow()

    for run in runs:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            report.skipped.append("(missing run_id)")
            continue
        safe_id = _safe(run_id)
        run_dir = experiment / safe_id
        for sub in ("metrics", "params", "tags"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)

        started = _millis(run.get("created_at") or run.get("start_time"), moment)
        metrics: dict[str, Any] = dict(run.get("metrics") or {})
        params: dict[str, Any] = dict(run.get("params") or {})
        tags: dict[str, Any] = {"run_kind": run.get("kind", ""), "strategy": run.get("strategy", "")}
        if run.get("git_commit"):
            tags["git_commit"] = run["git_commit"]
        if run.get("data_hash"):
            tags["data_hash"] = run["data_hash"]
        if run.get("notes"):
            tags["notes"] = run["notes"]
        if run.get("split"):
            params["split"] = run["split"]
        if run.get("seed") is not None:
            params["seed"] = run["seed"]

        (run_dir / "meta.yaml").write_text(
            f"run_uuid: {safe_id}\n"
            f"run_id: {safe_id}\n"
            f"experiment_id: {experiment_id}\n"
            f"user_id: tradebot\n"
            f"status: 3\n"
            f"start_time: {started}\n"
            f"end_time: {started}\n"
            f"artifact_uri: file://{run_dir.resolve()}/artifacts\n"
            "lifecycle_stage: active\n"
        )

        for key, value in metrics.items():
            number = _numeric(value)
            if number is None:
                # MLflow would reject this as a metric; a tag keeps the information.
                tags[f"metric_{key}"] = value
                report.runs_as_tags += 1
                continue
            (run_dir / "metrics" / _safe(key)).write_text(f"{started} {number!r} 0\n")
            report.metrics += 1
        for key, value in params.items():
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value, default=str)
            (run_dir / "params" / _safe(key)).write_text(str(value))
            report.params += 1
        for key, value in tags.items():
            if value in (None, ""):
                continue
            text = str(value).replace("\n", " ")
            (run_dir / "tags" / _safe(key)).write_text(text)
            report.tags += 1
        report.runs += 1

    return report


def export_from_store(store: Any, root: str | Path, *, limit: int = 500, name: str = "tradebot") -> ExportReport:
    """Export whatever the experiment log holds."""
    return export_runs(store.experiments(limit=limit), root, name=name)


__all__ = ["ExportReport", "export_from_store", "export_runs", "write_experiment"]
