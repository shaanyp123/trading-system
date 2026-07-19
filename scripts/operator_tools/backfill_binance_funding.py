"""scripts/operator_tools/backfill_binance_funding.py — one-shot proxy backfill.

Optional companion to the daily ``services/data/binance_funding_proxy.py``
logger (gate B3): backfills historical settled Binance USDT-perp funding
rates into ``funding_rates`` (``source='binance_proxy'``) for a date
range, e.g. to arm the B3 comparison window on day one instead of
waiting for the daily job to accumulate it, or to extend the series for
the §4 quarterly revalidation.

Pages the same PUBLIC ``GET /fapi/v1/fundingRate`` endpoint the daily
job uses (no API key; ``data.binance.vision`` bulk zips cover deeper
history but the REST endpoint serves the full funding archive for
BTC/ETH, so no zip parsing is needed). Every insert is idempotent
(``ON CONFLICT (product_id, observed_at_utc) DO NOTHING``) — re-running
any range re-inserts nothing.

Telemetry-only: writes the append-only ``funding_rates`` table
(consumed by gate B3 alone); touches no risk/order/audit surface.
Dry-run by default; pass ``--execute`` to insert.

A02: ``scripts/operator_tools/**`` is the §2.3 hot-fix lane. [A27]:
endpoint contract + rate-limit fact-checks live in
``deploy/binance_funding_proxy/README.md`` (shared with the daily job).

Run inside the api container (where DB access lives)::

    docker exec -i trading-api-1 /opt/venv/bin/python -m \\
      scripts.operator_tools.backfill_binance_funding \\
      --start 2026-06-01 --end 2026-07-19 --execute
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.data.binance_funding_proxy import (
    BINANCE_FUNDING_INTERVAL_HOURS,
    DEFAULT_FAPI_BASE_URL,
    FUNDING_FETCH_LIMIT,
    FUNDING_SOURCE_BINANCE_PROXY,
    HttpxBinanceFundingClient,
    parse_funding_entry,
    proxy_symbols_for_spot_products,
)
from services.data.coinbase_market_data import SPOT_SIGNAL_PRODUCT_IDS

#: In-container env var carrying the SQLAlchemy async URL (config.py
#: ``API_`` prefix); the bare name is accepted as a dev fallback.
DATABASE_URL_ENVS: Final[tuple[str, ...]] = ("API_DATABASE_URL", "DATABASE_URL")

#: 1000 settlements x 8 h ≈ 333 days per page — pagination advances
#: startTime past the last row of each page until the range is covered.
_PAGE_SPAN_GUARD: Final[int] = FUNDING_FETCH_LIMIT


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_binance_funding",
        description=(
            "Backfill settled Binance USDT-perp funding rates into "
            "funding_rates (source='binance_proxy'; idempotent)."
        ),
    )
    parser.add_argument(
        "--start", required=True, type=date.fromisoformat, help="Range start (UTC, YYYY-MM-DD)."
    )
    parser.add_argument(
        "--end",
        required=True,
        type=date.fromisoformat,
        help="Range end (UTC, YYYY-MM-DD, inclusive).",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help=(
            "Binance symbol to backfill (repeatable). Default: derived "
            "from the spot signal universe (BTCUSDT, ETHUSDT)."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_FAPI_BASE_URL,
        help="Binance fapi base URL (tests/mirrors only).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually INSERT rows. Default is a dry run (fetch + count only).",
    )
    return parser


def _resolve_database_url() -> str | None:
    for env in DATABASE_URL_ENVS:
        value = os.environ.get(env, "").strip()
        if value:
            return value
    return None


async def _fetch_range(
    client: HttpxBinanceFundingClient,
    symbol: str,
    *,
    start: datetime,
    end: datetime,
) -> list[Any]:
    """Page the funding archive for [start, end]; ascending, deduped."""
    observations: dict[datetime, Any] = {}
    cursor_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cursor_ms <= end_ms:
        rows = await client.get_funding_history(symbol, start_time_ms=cursor_ms)
        parsed = [
            obs
            for row in rows
            if (obs := parse_funding_entry(row)) is not None and obs.symbol == symbol
        ]
        in_range = [obs for obs in parsed if int(obs.funding_time_utc.timestamp() * 1000) <= end_ms]
        for obs in in_range:
            observations[obs.funding_time_utc] = obs
        if len(rows) < _PAGE_SPAN_GUARD or not parsed:
            break  # final (partial) page
        last_ms = max(int(obs.funding_time_utc.timestamp() * 1000) for obs in parsed)
        if last_ms < cursor_ms + 1:
            break  # defensive: no forward progress
        cursor_ms = last_ms + 1
    return [observations[k] for k in sorted(observations)]


async def _run(args: argparse.Namespace) -> int:
    if args.end < args.start:
        print(f"ERROR: --end {args.end} precedes --start {args.start}", file=sys.stderr)
        return 2
    symbols = (
        tuple(args.symbol)
        if args.symbol
        else proxy_symbols_for_spot_products(SPOT_SIGNAL_PRODUCT_IDS)
    )
    client = HttpxBinanceFundingClient(base_url=args.base_url)
    start_dt = datetime(args.start.year, args.start.month, args.start.day, tzinfo=UTC)
    end_dt = datetime(args.end.year, args.end.month, args.end.day, tzinfo=UTC) + timedelta(days=1)

    session_factory: async_sessionmaker[Any] | None = None
    engine = None
    if args.execute:
        database_url = _resolve_database_url()
        if database_url is None:
            print(
                f"ERROR: none of {DATABASE_URL_ENVS} set — run inside the api container.",
                file=sys.stderr,
            )
            return 5
        engine = create_async_engine(database_url, future=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

    exit_code = 0
    try:
        for symbol in symbols:
            observations = await _fetch_range(client, symbol, start=start_dt, end=end_dt)
            print(f"{symbol}: {len(observations)} settlements fetched [{args.start} .. {args.end}]")
            if not args.execute or session_factory is None:
                continue
            new_rows = 0
            async with session_factory() as session:
                async with session.begin():
                    for obs in observations:
                        result = await session.execute(
                            text(
                                "INSERT INTO funding_rates ("
                                "    product_id, observed_at_utc, rate_per_interval,"
                                "    interval_hours, mark_price, source"
                                ") VALUES ("
                                "    :product_id, :observed_at, :rate,"
                                "    :interval_hours, :mark_price, :source"
                                ") ON CONFLICT (product_id, observed_at_utc) DO NOTHING"
                            ),
                            {
                                "product_id": obs.symbol,
                                "observed_at": obs.funding_time_utc,
                                "rate": obs.funding_rate,
                                "interval_hours": BINANCE_FUNDING_INTERVAL_HOURS,
                                "mark_price": obs.mark_price,
                                "source": FUNDING_SOURCE_BINANCE_PROXY,
                            },
                        )
                        rowcount = getattr(result, "rowcount", 0)
                        if isinstance(rowcount, int) and rowcount > 0:
                            new_rows += 1
            print(f"{symbol}: {new_rows} new rows inserted (rest already present)")
    except Exception as exc:  # operator tool: fail loud, never half-silent
        print(f"ERROR: backfill failed: {exc!r}", file=sys.stderr)
        exit_code = 1
    finally:
        if engine is not None:
            await engine.dispose()
    if not args.execute:
        print("DRY RUN — no rows written. Re-run with --execute to insert.")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
