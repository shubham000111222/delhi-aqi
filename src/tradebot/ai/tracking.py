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
        # a caller's own tags (session mode, kill level, feeds) must survive the export
        tags.update({str(k): v for k, v in dict(run.get("tags") or {}).items()})
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
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            text = str(value).replace("\n", " ")
            (run_dir / "tags" / _safe(key)).write_text(text)
            report.tags += 1
        report.runs += 1

    return report


def export_from_store(store: Any, root: str | Path, *, limit: int = 500, name: str = "tradebot") -> ExportReport:
    """Export whatever the experiment log holds."""
    return export_runs(store.experiments(limit=limit), root, name=name)


def session_run(
    *,
    run_id: str,
    store: Any,
    status: dict[str, Any],
    started: datetime,
    mode: str = "paper",
    capital: Any = None,
    extra_params: dict[str, Any] | None = None,
    extra_tags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn a finished trading session into one MLflow run.

    A session is not a backtest cell, so the metrics here are the ones an operator reads
    at the end of a day (equity, realised P&L, fees, trade count, win rate) and the tags
    say what the run *was* (paper or live, kill level at exit). Numeric-only metric keys
    are the caller's job to respect; `export_runs` moves anything else to a tag.
    """
    stats = (status.get("stats") or {}) if status else {}
    closed = int(stats.get("trades_closed") or 0)
    # the engine counts trades, not winners, so honest win rate needs the closed positions
    wins = 0
    try:
        wins = sum(1 for position in store.closed_positions(limit=10_000) if float(position["pnl"]) > 0)
    except Exception:  # noqa: BLE001 - a store without that query must not break a log
        wins = 0
    metrics: dict[str, Any] = {
        "equity": status.get("equity"),
        "cash": status.get("cash"),
        "realized_pnl": status.get("realized_pnl"),
        "unrealized_pnl": status.get("unrealized_pnl"),
        "fees": status.get("fees"),
        "trades_closed": closed,
        "win_rate": (wins / closed) if closed else 0.0,
        "signals": stats.get("signals"),
        "vetoes": stats.get("vetoes"),
        "orders": stats.get("orders"),
        "fills": stats.get("fills"),
    }
    params: dict[str, Any] = {"mode": mode}
    if capital is not None:
        params["capital"] = capital
    params.update(extra_params or {})
    tags: dict[str, Any] = {
        "kind": "session",
        "mode": mode,
        "kill_level": (status.get("kill_state") or {}).get("level", "L0"),
    }
    tags.update(extra_tags or {})
    return {
        "run_id": run_id,
        "kind": "session",
        "created_at": started.isoformat(),
        "metrics": {key: value for key, value in metrics.items() if value is not None},
        "params": params,
        "tags": tags,
    }


def read_runs(root: str | Path, *, limit: int = 200) -> list[dict[str, Any]]:
    """Read a file store back into plain run dicts.

    This is the other half of the export: the bot's SQLite log stays the source of truth,
    but a run written by a teammate's `mlflow server` (file backend) or by an older
    checkout has to be readable before it can be compared with anything. Missing or
    malformed files are skipped rather than raising - a half-written store is a normal
    accident, not a reason for a stack trace.
    """
    base = Path(root)
    if not base.exists():
        return []
    runs: list[dict[str, Any]] = []
    for experiment in sorted(p for p in base.iterdir() if p.is_dir()):
        name = _experiment_name(experiment)
        for run_dir in sorted(p for p in experiment.iterdir() if p.is_dir()):
            if not (run_dir / "meta.yaml").exists():
                continue
            metrics: dict[str, float] = {}
            for path in sorted((run_dir / "metrics").glob("*")) if (run_dir / "metrics").exists() else []:
                parts = path.read_text().split()
                if len(parts) < 2:
                    continue
                try:
                    metrics[path.name] = float(parts[-2] if len(parts) > 2 else parts[1])
                except ValueError:
                    continue
            params = _read_dir(run_dir / "params")
            tags = _read_dir(run_dir / "tags")
            meta = _read_meta(run_dir / "meta.yaml")
            runs.append(
                {
                    "run_id": meta.get("run_id") or run_dir.name,
                    "experiment": name,
                    "start_time": meta.get("start_time"),
                    "metrics": metrics,
                    "params": params,
                    "tags": tags,
                }
            )
            if len(runs) >= limit:
                return runs
    return runs


def _read_dir(directory: Path) -> dict[str, str]:
    if not directory.exists():
        return {}
    out: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            try:
                out[path.name] = path.read_text().strip()
            except OSError:  # pragma: no cover - unreadable file in a foreign store
                continue
    return out


def _read_meta(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:  # pragma: no cover
        return out
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


def _experiment_name(directory: Path) -> str:
    meta = directory / "meta.yaml"
    if meta.exists():
        return _read_meta(meta).get("name", directory.name)
    return directory.name


__all__ = [
    "ExportReport",
    "export_from_store",
    "export_runs",
    "read_runs",
    "session_run",
    "write_experiment",
]
