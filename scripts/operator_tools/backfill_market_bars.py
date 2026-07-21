"""scripts/operator_tools/backfill_market_bars.py — one-shot OHLCV backfill.

Companion to the market-data worker's daily/hourly ``market_bars``
capture (operator directive 2026-07-20, agentic-refinement data
capture): backfills historical daily (and optionally hourly) OHLCV —
INCLUDING VOLUME — into ``market_bars`` for a date range, so the durable
bar record starts at the venue's earliest history instead of at the
capture jobs' arming date. The v2.1 volume study's out-of-sample plan
(research/crypto_perps_v2/) reads this table.

Pages the same PUBLIC ``/api/v3/brokerage/market/products/{id}/candles``
endpoint the worker uses (no API key; ~350 candles/request cap — this
script pages in 300-bar windows). Every insert is idempotent
(``ON CONFLICT (product_id, granularity, bar_start_utc) DO NOTHING``) —
re-running any range re-inserts nothing.

Telemetry-only: writes the append-only ``market_bars`` table; touches no
risk/order/audit surface. The signal engine keeps fetching its own bars
from the venue — this table is the analysis record, not a trading input.
Dry-run by default; pass ``--execute`` to insert.

Coverage notes (venue truth, unverified depths are [A27]-flagged in the
runbook): Coinbase spot ``BTC-USD`` daily candles reach back to ~2015,
``ETH-USD`` to ~2016. CDE perp products (``--include-perps``, discovered
at runtime per [A13]) have only weeks-to-months of history — the venue
returns what it has; short coverage is expected, not an error. Hourly
backfill (``--granularity hourly`` / ``both``) is far heavier
(24x the requests) — default lookback is bounded by ``--start``.

A02: ``scripts/operator_tools/**`` is the §2.3 hot-fix lane.

Run inside the api container (where DB access lives)::

    docker exec -i trading-api-1 /opt/venv/bin/python -m \\
      scripts.operator_tools.backfill_market_bars \\
      --start 2016-01-01 --end 2026-07-19 --execute

    # add venue perp bars + hourly spot bars for the recent window:
    docker exec -i trading-api-1 /opt/venv/bin/python -m \\
      scripts.operator_tools.backfill_market_bars \\
      --start 2026-06-01 --granularity both --include-perps --execute
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.data.coinbase_market_data import (
    CANDLES_PER_REQUEST,
    SPOT_SIGNAL_PRODUCT_IDS,
    CandleRow,
    HttpxCoinbaseRestClient,
    discover_perp_products,
    parse_candle_row,
    spot_base_assets,
)

DATABASE_URL_ENVS: Final[tuple[str, ...]] = ("API_DATABASE_URL", "DATABASE_URL")

_GRANULARITY_SECONDS: Final[dict[str, int]] = {"ONE_DAY": 86400, "ONE_HOUR": 3600}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_market_bars",
        description="Idempotent historical OHLCV (incl. volume) backfill into market_bars.",
    )
    parser.add_argument("--start", required=True, help="first bar date (YYYY-MM-DD, UTC)")
    parser.add_argument(
        "--end",
        default=None,
        help="last bar date inclusive (YYYY-MM-DD, UTC; default: yesterday)",
    )
    parser.add_argument(
        "--products",
        default=",".join(SPOT_SIGNAL_PRODUCT_IDS),
        help="comma-separated product ids (default: the spot signal products)",
    )
    parser.add_argument(
        "--include-perps",
        action="store_true",
        help="also backfill the discovered CDE perp products on the traded base assets ([A13])",
    )
    parser.add_argument(
        "--granularity",
        choices=("daily", "hourly", "both"),
        default="daily",
        help="bar granularity to backfill (hourly is 24x the requests; default daily)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually insert (default is a dry-run that only counts fetched bars)",
    )
    return parser


def _database_url() -> str | None:
    for env in DATABASE_URL_ENVS:
        url = os.environ.get(env)
        if url:
            return url
    return None


async def _fetch_range(
    rest: HttpxCoinbaseRestClient,
    product_id: str,
    *,
    granularity: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[CandleRow]:
    """Page [start_utc, end_utc) in CANDLES_PER_REQUEST windows, ascending."""
    step = timedelta(seconds=_GRANULARITY_SECONDS[granularity] * CANDLES_PER_REQUEST)
    by_start: dict[datetime, CandleRow] = {}
    window_start = start_utc
    while window_start < end_utc:
        window_end = min(window_start + step, end_utc)
        raw_candles = await rest.get_candles(
            product_id,
            start_unix=int(window_start.timestamp()),
            end_unix=int(window_end.timestamp()),
            granularity=granularity,
        )
        for raw in raw_candles:
            row = parse_candle_row(product_id, raw, granularity=granularity)
            if row is not None and start_utc <= row.bar_start_utc < end_utc:
                by_start[row.bar_start_utc] = row
        window_start = window_end
    return [by_start[k] for k in sorted(by_start)]


async def _insert_rows(
    session_factory: async_sessionmaker[Any],
    rows: list[CandleRow],
    *,
    captured_at_utc: datetime,
) -> int:
    inserted = 0
    async with session_factory() as session:
        async with session.begin():
            for row in rows:
                result = await session.execute(
                    text(
                        "INSERT INTO market_bars ("
                        "    product_id, granularity, bar_start_utc,"
                        "    open, high, low, close, volume,"
                        "    source, captured_at_utc"
                        ") VALUES ("
                        "    :product_id, :granularity, :bar_start,"
                        "    :open, :high, :low, :close, :volume,"
                        "    'coinbase_advanced', :captured_at"
                        ") ON CONFLICT (product_id, granularity, bar_start_utc) DO NOTHING"
                    ),
                    {
                        "product_id": row.product_id,
                        "granularity": row.granularity,
                        "bar_start": row.bar_start_utc,
                        "open": row.open,
                        "high": row.high,
                        "low": row.low,
                        "close": row.close,
                        "volume": row.volume,
                        "captured_at": captured_at_utc,
                    },
                )
                inserted += int(getattr(result, "rowcount", 0) or 0)
    return inserted


async def _run(args: argparse.Namespace) -> int:
    try:
        start_date = date.fromisoformat(args.start)
        end_date = (
            date.fromisoformat(args.end)
            if args.end
            else (datetime.now(tz=UTC) - timedelta(days=1)).date()
        )
    except ValueError as exc:
        print(f"ERROR: bad date: {exc}", file=sys.stderr)
        return 1
    if end_date < start_date:
        print("ERROR: --end before --start", file=sys.stderr)
        return 1

    granularities = {
        "daily": ("ONE_DAY",),
        "hourly": ("ONE_HOUR",),
        "both": ("ONE_DAY", "ONE_HOUR"),
    }[args.granularity]
    start_utc = datetime.combine(start_date, time(0, 0), tzinfo=UTC)
    # end bound is EXCLUSIVE of the day after end_date, so end_date's
    # bars (daily bar-start = midnight; hourly through 23:00) are included
    end_utc = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)

    rest = HttpxCoinbaseRestClient()
    product_ids = [p.strip() for p in str(args.products).split(",") if p.strip()]
    if args.include_perps:
        traded = spot_base_assets(SPOT_SIGNAL_PRODUCT_IDS)
        try:
            products: list[Mapping[str, Any]] = list(await rest.get_future_products())
            perps = discover_perp_products(products)
        except Exception as exc:
            print(f"ERROR: perp discovery failed: {exc}", file=sys.stderr)
            return 1
        product_ids += [
            p.product_id
            for p in perps
            if p.base_asset in traded and p.product_id not in product_ids
        ]

    engine = None
    session_factory: async_sessionmaker[Any] | None = None
    if args.execute:
        database_url = _database_url()
        if database_url is None:
            print(
                f"ERROR: none of {DATABASE_URL_ENVS} set — run inside the api container.",
                file=sys.stderr,
            )
            return 1
        engine = create_async_engine(database_url, future=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

    captured_at = datetime.now(tz=UTC)
    total_fetched = 0
    total_inserted = 0
    for product_id in product_ids:
        for granularity in granularities:
            try:
                rows = await _fetch_range(
                    rest,
                    product_id,
                    granularity=granularity,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )
            except Exception as exc:
                # Venue may refuse a granularity/range for a product (esp.
                # young perps) — report and keep going; idempotent re-runs
                # can retry any subset.
                print(f"WARN: fetch failed {product_id} {granularity}: {exc}", file=sys.stderr)
                continue
            total_fetched += len(rows)
            if session_factory is not None and rows:
                inserted = await _insert_rows(session_factory, rows, captured_at_utc=captured_at)
                total_inserted += inserted
                print(f"{product_id} {granularity}: fetched={len(rows)} inserted={inserted}")
            else:
                print(f"{product_id} {granularity}: fetched={len(rows)} (dry-run)")

    if engine is not None:
        await engine.dispose()
    mode = "EXECUTED" if args.execute else "DRY-RUN"
    print(f"{mode}: products={len(product_ids)} fetched={total_fetched} inserted={total_inserted}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
