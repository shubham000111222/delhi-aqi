"""Explicit research CLI: python -m tradebot.backtest.timemachine_cli --help."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from ..config import load_config
from ..core.clock import parse_iso
from ..core.models import Candle
from ..data.archive import Archive
from .evidence_export import publish_evidence
from .timemachine_engine_study import run_engine_study
from .timemachine_study import StudyWindow
from .timemachine_windows import ResearchWindow, run_window_studies, validate_windows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Isolated Time Machine four-engine research study; never enables trading')
    parser.add_argument('--config-dir', type=Path, default=Path('config'))
    parser.add_argument('--archive-root', type=Path, required=True)
    parser.add_argument('--start', help='aware ISO timestamp')
    parser.add_argument('--end', help='exclusive aware ISO timestamp')
    parser.add_argument('--holdout-start', required=True, help='untouched final holdout boundary')
    parser.add_argument('--fee-rate', type=float, required=True, help='checker fee assumption per leg')
    parser.add_argument('--slippage-rate', type=float, required=True, help='checker adverse slippage assumption')
    parser.add_argument('--windows-file', type=Path, help='predeclared JSON list of 3..24 start/end windows; replaces --start/--end')
    parser.add_argument('--max-bars', type=int, default=288)
    parser.add_argument('--current-volatility', action='store_true', help='use fixed trailing24h closed5m volatility ratio')
    parser.add_argument('--cost-stress', action='store_true', help='rerun all four variants at 2x modeled costs')
    parser.add_argument('--output', type=Path, required=True, help='new JSON evidence file; refuses overwrite')
    args = parser.parse_args(argv)
    try:
        # parse_iso normalizes naive values; reject them before normalization.
        from datetime import datetime

        def aware(value: str) -> datetime:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError('study timestamps must be timezone aware')
            return parse_iso(value)

        holdout = aware(args.holdout_start)
        windows = None
        if args.windows_file:
            if args.start or args.end:
                raise ValueError('windows-file cannot be mixed with start/end')
            if args.windows_file.stat().st_size > 16384:
                raise ValueError('window manifest exceeds 16KiB limit')
            manifest = json.loads(args.windows_file.read_text(encoding='utf-8'))
            if not isinstance(manifest, list):
                raise ValueError('window manifest must be a list')
            windows = validate_windows([ResearchWindow(aware(row['start']), aware(row['end']))
                                        for row in manifest], holdout)
            start, end = windows[0].start, windows[-1].end
        else:
            if not args.start or not args.end:
                raise ValueError('supply start/end or windows-file')
            start, end = aware(args.start), aware(args.end)
            StudyWindow(start, end, holdout, 'pending', 'pending', 1.).validate()
        config = load_config(args.config_dir)
        if args.output.exists():
            raise ValueError('output exists; refusing overwrite')
        archive = Archive(args.archive_root)

        def load(timeframe: str, beginning: datetime) -> dict[str, list[Candle]]:
            result = {}
            for symbol in config.symbol_names():
                rows = archive.query("SELECT * FROM candles WHERE symbol=? AND timeframe=? "
                                     "AND ts>=? AND ts<? ORDER BY ts LIMIT 500001",
                                     [symbol, timeframe, beginning, end])
                if len(rows) > 500000:
                    raise ValueError('archive input exceeds bounded research load')
                result[symbol] = [Candle(symbol, timeframe, parse_iso(str(row['ts'])),
                                         *(Decimal(str(row[k])) for k in
                                           ('open', 'high', 'low', 'close', 'volume'))) for row in rows]
            return result

        execution = load(config.settings.base_timeframe, start)
        history = load('5m', start-timedelta(days=60))
        if windows is None:
            result = asyncio.run(run_engine_study(config, execution, history, start=start, end=end,
                                                  final_holdout_start=holdout, fee_rate=args.fee_rate,
                                                  slippage_rate=args.slippage_rate, max_bars=args.max_bars,
                                                  cost_stress=args.cost_stress,
                                                  volatility_scale=None if args.current_volatility else 1.))
        else:
            result = asyncio.run(run_window_studies(config, execution, history, windows=windows,
                                                   final_holdout_start=holdout, fee_rate=args.fee_rate,
                                                   slippage_rate=args.slippage_rate, max_bars=args.max_bars,
                                                   cost_stress=args.cost_stress,
                                                  volatility_scale=None if args.current_volatility else 1.))
        publish_evidence(args.output, result)
        print(f"Research complete={result['complete']}; filter_enabled=False; report={args.output}")
        return 0 if result['complete'] else 2
    except Exception as exc:  # noqa: BLE001 - CLI never converts failure into enablement
        print(f'Research refused: {type(exc).__name__}: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
