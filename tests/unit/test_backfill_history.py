"""`backfill-history`: months of candles in one command, resumable, and honest.

The command exists because a six-month backfill is six chances to lose a page, and a
silent hole in the archive poisons everything computed downstream. These tests pin the
three behaviours that make it trustworthy: it chunks, it resumes, and it refuses to call
a partial chunk complete.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tradebot import main as cli
from tradebot.config import Config, load_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def history_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    config = load_config(
        ROOT / "config",
        environ={},
        overrides={
            "data_dir": str(tmp_path / "data"),
            "report": {"dir": str(tmp_path / "reports")},
        },
    )
    config.ensure_dirs()
    monkeypatch.setattr(cli, "_config", lambda args: config)
    # Fixed closed historical endpoint: wall-clock minute rollover must not change
    # how many demo candles the CLI expects while the test is paging.
    monkeypatch.setattr(cli, "utcnow", lambda: datetime(2024, 11, 2, tzinfo=UTC))
    return config


def run_cli(argv: list[str]) -> int:
    return cli.main(argv)


def state_of(config: Config) -> dict[str, Any]:
    path = Path(config.settings.data_dir) / "backfill-history.json"
    return json.loads(path.read_text())


class TestBackfillHistory:
    def test_it_fills_the_window_and_records_every_chunk(self, history_cfg):
        code = run_cli(
            ["backfill-history", "--source", "demo", "--months", "1",
             "--symbols", "BTC/USDT", "--page-limit", "1000"]
        )

        assert code == 0
        state = state_of(history_cfg)
        # the window is month-aligned, so "one month back" is October plus the part of
        # November that has happened - and each of those is its own resume unit
        assert len(state["chunks"]) == 2
        for chunk in state["chunks"].values():
            assert chunk["complete"] is True
            assert chunk["kept"] == chunk["expected_bars"] > 0, \
                "every chunk that has closed must be exactly as long as it could be"
            assert chunk["gaps"] == []

    def test_a_complete_chunk_is_not_fetched_again(self, history_cfg, capsys):
        argv = [
            "backfill-history", "--source", "demo", "--months", "1",
            "--symbols", "BTC/USDT", "--page-limit", "1000",
        ]
        assert run_cli(argv) == 0
        capsys.readouterr()
        before = sorted((Path(history_cfg.settings.archive_dir)).rglob("*.parquet"))

        assert run_cli(argv) == 0
        out = capsys.readouterr().out
        after = sorted((Path(history_cfg.settings.archive_dir)).rglob("*.parquet"))

        assert "skipped" in out
        assert before == after, "a resumed run must not rewrite what it already has"

    def test_a_partial_chunk_exits_non_zero_and_is_retried(self, history_cfg, capsys):
        capped = [
            "backfill-history", "--source", "demo", "--months", "1",
            "--symbols", "BTC/USDT", "--page-limit", "1000", "--max-pages", "2",
        ]
        assert run_cli(capped) == 4, "a truncated chunk must fail the command"
        state = state_of(history_cfg)
        assert next(iter(state["chunks"].values()))["complete"] is False
        capsys.readouterr()

        full = [
            "backfill-history", "--source", "demo", "--months", "1",
            "--symbols", "BTC/USDT", "--page-limit", "1000", "--retry-incomplete",
        ]
        assert run_cli(full) == 0
        state = state_of(history_cfg)
        assert next(iter(state["chunks"].values()))["complete"] is True

    def test_the_json_summary_is_the_thing_a_script_reads(self, history_cfg, capsys):
        code = run_cli(
            ["backfill-history", "--source", "demo", "--months", "1",
             "--symbols", "BTC/USDT", "--page-limit", "1000", "--json"]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["complete"] is True
        assert payload["incomplete"] == []
        assert payload["gap_total"] == 0
        assert payload["kept_total"] > 40_000
        assert payload["rows"][0]["skipped"] is False

    def test_a_backwards_window_is_refused(self, history_cfg):
        code = run_cli(
            ["backfill-history", "--source", "demo", "--from", "2026-05-01T00:00:00Z",
             "--to", "2026-04-01T00:00:00Z", "--symbols", "BTC/USDT"]
        )
        assert code == 2


@pytest.mark.parametrize('end,months,expected', [
    ('2024-02-29T12:00:00Z', 12, '2023-02-01'),
    ('2024-02-29T12:00:00Z', 24, '2022-02-01'),
    ('2024-01-15T00:00:00Z', 13, '2022-12-01'),
    ('2024-03-01T00:00:00Z', 1, '2024-02-01'),
])
def test_calendar_months_span_multiple_years(history_cfg, monkeypatch, end, months, expected):
    captured = []

    def chunks(start, stop):
        captured.append((start, stop))
        return []

    monkeypatch.setattr(cli, '_month_chunks', chunks)
    assert run_cli(['backfill-history', '--source', 'demo', '--months', str(months),
                    '--to', end, '--symbols', 'BTC/USDT']) == 0
    assert captured[0][0].date().isoformat() == expected


@pytest.mark.parametrize('months', ['0', '-1', '999999'])
def test_invalid_calendar_months_fail_without_backfill(history_cfg, months, capsys):
    assert run_cli(['backfill-history', '--source', 'demo', '--months', months,
                    '--to', '2024-02-29T12:00:00Z']) == 2
    assert 'error:' in capsys.readouterr().err
    assert not (history_cfg.settings.data_dir / 'backfill-history.json').exists()
