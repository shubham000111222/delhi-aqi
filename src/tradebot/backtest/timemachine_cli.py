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
from .timemachine_engine_study import run_engine_study
from .timemachine_study import StudyWindow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Isolated Time Machine four-engine research study; never enables trading')
    parser.add_argument('--config-dir', type=Path, default=Path('config'))
    parser.add_argument('--archive-root', type=Path, required=True)
    parser.add_argument('--start', required=True, help='aware ISO timestamp')
    parser.add_argument('--end', required=True, help='exclusive aware ISO timestamp')
    parser.add_argument('--holdout-start', required=True, help='untouched final holdout boundary')
    parser.add_argument('--fee-rate', type=float, required=True, help='checker fee assumption per leg')
    parser.add_argument('--slippage-rate', type=float, required=True, help='checker adverse slippage assumption')
    parser.add_argument('--max-bars', type=int, default=288)
    parser.add_argument('--cost-stress', action='store_true', help='rerun all four variants at 2x modeled costs')
    parser.add_argument('--output', type=Path, required=True, help='new JSON evidence file; refuses overwrite')
    args = parser.parse_args(argv)
    try:
        # parse_iso normalizes naive values; reject them before normalization.
        from datetime import datetime

        stamps = [datetime.fromisoformat(value.replace('Z', '+00:00'))
                  for value in (args.start, args.end, args.holdout_start)]
        StudyWindow(stamps[0], stamps[1], stamps[2], 'pending', 'pending', 1.).validate()
        start, end, holdout = [parse_iso(value) for value in (args.start, args.end, args.holdout_start)]
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
        result = asyncio.run(run_engine_study(config, execution, history, start=start, end=end,
                                              final_holdout_start=holdout, fee_rate=args.fee_rate,
                                              slippage_rate=args.slippage_rate, max_bars=args.max_bars,
                                              cost_stress=args.cost_stress))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, indent=2, default=str, allow_nan=False)
        print(f"Research complete={result['complete']}; filter_enabled=False; report={args.output}")
        return 0 if result['complete'] else 2
    except Exception as exc:  # noqa: BLE001 - CLI never converts failure into enablement
        print(f'Research refused: {type(exc).__name__}: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
