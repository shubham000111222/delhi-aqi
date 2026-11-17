"""Candle and order-book archive: partitioned Parquet, queried with DuckDB.

Two jobs, both from the plan (§4, §18.1):

* keep the candles the system has seen, so a backtest can run on real history instead of
  a synthetic generator;
* keep recorded order books, because slippage assumptions are otherwise fiction.

Layout is Hive-style so DuckDB (and anything else) can prune by symbol and date:

    data/archive/candles/symbol=BTC-USDT/date=2024-01-01/candles.parquet
    data/archive/books/symbol=BTC-USDT/date=2024-01-01/books.parquet

Both libraries are optional (`pip install -e ".[data]"`): the trading engine never imports
this module, so a missing pyarrow cannot stop the bot.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.clock import utc
from ..core.models import Candle, OrderBook

CANDLE_SCHEMA = ("symbol", "timeframe", "ts", "open", "high", "low", "close", "volume")
BOOK_SCHEMA = ("symbol", "ts", "levels", "bids", "asks", "best_bid", "best_ask", "spread_bps")


def _require(name: str) -> Any:
    try:
        module = __import__(name, fromlist=["*"])
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            f'the Parquet archive needs {name}: pip install -e ".[data]"'
        ) from exc
    return module


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-")


def candle_row(candle: Candle) -> dict[str, Any]:
    """Decimals become strings: a float would silently round a price."""
    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "ts": utc(candle.ts),
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
    }


def book_row(book: OrderBook) -> dict[str, Any]:
    best_bid = book.best_bid
    best_ask = book.best_ask
    spread_bps = 0.0
    if best_bid and best_ask and best_bid > 0:
        spread_bps = float((best_ask - best_bid) / best_bid * 10_000)
    return {
        "symbol": book.symbol,
        "ts": utc(book.ts),
        "levels": len(book.bids) + len(book.asks),
        "bids": json.dumps([[str(level.price), str(level.qty)] for level in book.bids]),
        "asks": json.dumps([[str(level.price), str(level.qty)] for level in book.asks]),
        "best_bid": str(best_bid) if best_bid is not None else None,
        "best_ask": str(best_ask) if best_ask is not None else None,
        "spread_bps": spread_bps,
    }


class Archive:
    """Partitioned Parquet store for candles and books."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # --- paths ---------------------------------------------------------------
    def partition(self, kind: str, symbol: str, day: date) -> Path:
        # `sym=`/`day=`, not `symbol=`/`date=`: a hive column with the same name as a
        # column in the file silently shadows it, and every filtered query returns nothing.
        return self.root / kind / f"sym={_safe_symbol(symbol)}" / f"day={day.isoformat()}"

    # --- writes --------------------------------------------------------------
    def write_candles(self, candles: Sequence[Candle]) -> list[Path]:
        return self._write("candles", candles, candle_row, CANDLE_SCHEMA)

    def write_books(self, books: Sequence[OrderBook]) -> list[Path]:
        return self._write("books", books, book_row, BOOK_SCHEMA)

    def _write(self, kind: str, rows: Sequence[Any], to_row: Any, schema: tuple[str, ...]) -> list[Path]:
        if not rows:
            return []
        pa = _require("pyarrow")
        pq = _require("pyarrow.parquet")
        if not hasattr(pq, "write_table"):  # pragma: no cover - defensive
            raise RuntimeError('the Parquet archive needs pyarrow.parquet: pip install -e ".[data]"')
        by_partition: dict[tuple[str, date], list[dict[str, Any]]] = {}
        for item in rows:
            by_partition.setdefault((item.symbol, utc(item.ts).date()), []).append(to_row(item))

        written: list[Path] = []
        for (symbol, day), partition_rows in by_partition.items():
            directory = self.partition(kind, symbol, day)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{kind}.parquet"
            table = pa.Table.from_pylist(
                [{key: row[key] for key in schema} for row in partition_rows]
            )
            if path.exists():
                # append and de-duplicate on (symbol, timeframe/ts) so a re-run is idempotent
                existing = pq.read_table(path)
                table = pa.concat_tables([existing, table])
                keys = ["symbol", "timeframe", "ts"] if kind == "candles" else ["symbol", "ts"]
                table = _dedupe(pa, table, keys)
            pq.write_table(table, path, compression="zstd")
            written.append(path)
        return written

    # --- reads ---------------------------------------------------------------
    def query(self, sql: str, parameters: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """Run SQL against the archive; ``candles`` and ``books`` are views."""
        duckdb = _require("duckdb")
        connection = duckdb.connect()
        try:
            self._create_views(connection)
            rows = connection.execute(sql, parameters or []).fetchall()
            columns = [description[0] for description in connection.description]
            return [dict(zip(columns, row, strict=False)) for row in rows]
        finally:
            connection.close()

    def _create_views(self, connection: Any) -> None:
        for view, kind in (("candles", "candles"), ("books", "books")):
            pattern = self.root / kind / "**" / "*.parquet"
            if not any(self.root.glob(f"{kind}/**/*.parquet")):
                connection.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT NULL AS empty WHERE false")
                continue
            escaped_pattern = str(pattern).replace("'", "''")
            connection.execute(
                f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT * FROM read_parquet('{escaped_pattern}', hive_partitioning=true)"
            )

    def candles(
        self, *, symbol: str | None = None, timeframe: str | None = None, limit: int = 10_000
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        if symbol:
            clauses.append(f"symbol = '{symbol}'")
        if timeframe:
            clauses.append(f"timeframe = '{timeframe}'")
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        return self.query(f"SELECT * FROM candles {where}ORDER BY ts LIMIT {int(limit)}")

    def spread_stats(self, *, symbol: str | None = None) -> dict[str, Any]:
        """Empirical spread distribution: the input to an honest slippage model."""
        where = f"WHERE symbol = '{symbol}' " if symbol else ""
        rows = self.query(
            "SELECT count(*) AS observations, avg(spread_bps) AS mean_spread_bps, "
            f"quantile_cont(spread_bps, 0.5) AS median_spread_bps, "
            f"quantile_cont(spread_bps, 0.95) AS p95_spread_bps, "
            f"max(spread_bps) AS max_spread_bps FROM books {where}"
        )
        return rows[0] if rows else {}

    def coverage(self) -> dict[str, Any]:
        """What is in the archive, per kind: symbols, days, rows."""
        out: dict[str, Any] = {}
        for kind in ("candles", "books"):
            files = sorted(self.root.glob(f"{kind}/**/*.parquet"))
            symbols = sorted({path.parent.parent.name.split("=", 1)[1] for path in files})
            days = sorted({path.parent.name.split("=", 1)[1] for path in files})
            out[kind] = {
                "files": len(files),
                "symbols": symbols,
                "days": len(days),
                "first_day": days[0] if days else None,
                "last_day": days[-1] if days else None,
                "bytes": sum(path.stat().st_size for path in files),
            }
        return out

    # --- retention -----------------------------------------------------------
    def apply_retention(self, *, keep_days: int, kind: str = "books", now: date | None = None) -> list[Path]:
        """Drop partitions older than ``keep_days``; returns what was removed."""
        today = now or datetime.now().date()
        removed: list[Path] = []
        for path in sorted(self.root.glob(f"{kind}/**/*.parquet")):
            raw = path.parent.name.split("=", 1)[-1]
            try:
                day = date.fromisoformat(raw)
            except ValueError:  # pragma: no cover - unexpected layout
                continue
            if (today - day).days > keep_days:
                path.unlink()
                removed.append(path)
        return removed


def _dedupe(pa: Any, table: Any, keys: list[str]) -> Any:
    """Keep the last row per key without pulling the data into Python."""
    columns = ", ".join(keys)
    ordering = ", ".join(f"{key}" for key in keys)
    import duckdb

    connection = duckdb.connect()
    try:
        connection.register("incoming", table)
        return connection.sql(
            f"SELECT * EXCLUDE (rn) FROM ("
            f"  SELECT *, row_number() OVER (PARTITION BY {columns} ORDER BY {ordering}) AS rn"
            f"  FROM incoming) WHERE rn = 1"
        ).to_arrow_table()
    finally:
        connection.close()


@dataclass
class ArchiveStats:
    candles: int = 0
    books: int = 0
    bytes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"candles": self.candles, "books": self.books, "bytes": self.bytes}


def write_tape_to_archive(archive: Archive, events: Iterable[tuple[str, Any, Any]]) -> ArchiveStats:
    """Persist a recorded tape (`data.tape`) as candles and books."""
    candles: list[Candle] = []
    books: list[OrderBook] = []
    for kind, payload, _stamp in events:
        if kind == "bar" and isinstance(payload, Candle):
            candles.append(payload)
        elif kind == "book" and isinstance(payload, OrderBook):
            books.append(payload)
    files = archive.write_candles(candles) + archive.write_books(books)
    return ArchiveStats(
        candles=len(candles),
        books=len(books),
        bytes=sum(path.stat().st_size for path in files),
    )


_CANDLE_CACHE: dict[tuple[str, str, tuple[str, ...], int], dict[str, list[Candle]]] = {}


def read_candles(
    config: Any,
    *,
    root: str | Path | None = None,
    timeframe: str | None = None,
    limit: int = 5_000_000,
) -> dict[str, list[Candle]]:
    """Every archived candle for the configured symbols, ready for `ReplayFeed`.

    This is the bridge between `tradebot backfill` and the validation suite: without it
    the archive is a museum. Raises a plain-language error when the archive is empty or
    the Parquet extras are missing, because both are configuration problems, not crashes.
    """
    from ..core.clock import parse_iso

    directory = Path(root or (config.settings.archive_dir or Path(config.settings.data_dir) / "archive"))
    if not any(directory.glob("candles/**/*.parquet")):
        raise ValueError(
            f"no candles in {directory}: run `python -m tradebot backfill --days 30 --write` first"
        )
    wanted = timeframe or config.settings.base_timeframe
    key = (str(directory), wanted, tuple(config.symbol_names()), limit)
    cached = _CANDLE_CACHE.get(key)
    if cached is not None:
        return cached  # a walk-forward builds one engine per window; read the files once
    archive = Archive(directory)
    bars: dict[str, list[Candle]] = {}
    for symbol in config.symbol_names():
        rows = archive.candles(symbol=symbol, timeframe=wanted, limit=limit)
        if not rows:
            continue
        bars[symbol] = [
            Candle(
                symbol=str(row["symbol"]),
                timeframe=str(row["timeframe"]),
                ts=parse_iso(str(row["ts"])),
                open=decimal_of(row["open"]),
                high=decimal_of(row["high"]),
                low=decimal_of(row["low"]),
                close=decimal_of(row["close"]),
                volume=decimal_of(row["volume"]),
            )
            for row in rows
        ]
        bars[symbol].sort(key=lambda candle: candle.ts)
    if not bars:
        raise ValueError(
            f"{directory} has candles but none for {config.symbol_names()} at {wanted}"
        )
    _CANDLE_CACHE[key] = bars
    return bars


def decimal_of(value: Any) -> Decimal:
    return Decimal(str(value))
