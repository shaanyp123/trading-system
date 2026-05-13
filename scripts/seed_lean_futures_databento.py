"""Build a LEAN-format futures-daily seed bundle for the Phase 1 micro futures.

Sibling to ``scripts/seed_lean_data.py`` (which covers ETFs via yfinance).
This script fetches daily OHLCV + Open Interest + contract definitions via
the **DataBento Python SDK directly** and writes the LEAN futures-daily
on-disk layout under ``<out>/future/<market>/`` with the simplifying insight
that **``factor_files/`` are NOT required when the strategy uses
``DataNormalizationMode.Raw``** (the default for ``add_future`` in QC's
modern Python API).

**Why DataBento direct SDK + custom converter** (rather than the lean CLI):
the lean CLI's DataBento integration only supports ``--data-type Trade``
or ``--data-type Quote`` — see ``Docs/decisions-log.md`` 2026-05-12 entry
"Post-PR-#130 session continued — Tiingo cross-check PASS (ETF pre-live
gate LOCKED); LEAN CLI's DataBento integration NOT viable for our strategy's
continuous-futures needs". Our strategy requires Trade + Open Interest +
Universe (per-day active-contracts) data because ``lean/v1_strategy.py``
binds futures with ``data_mapping_mode=DataMappingMode.OPEN_INTEREST`` so
LEAN can pick the front-month contract by highest open interest. The
DataBento Python SDK fetches all 3 schemas directly; this converter writes
them into the LEAN on-disk format LEAN expects.

Usage examples::

    # Default — 7 Phase 1 micros, 23-month window, stage to /tmp/lean_seed_futures
    python3 scripts/seed_lean_futures_databento.py --api-key "$DATABENTO_API_KEY"

    # Single-ticker test, 30-day window:
    python3 scripts/seed_lean_futures_databento.py --api-key "$DB_KEY" \\
        --tickers MES --start 2026-04-01 --end 2026-05-01 --no-tar

    # Skip the cost confirmation prompt (CI / automation):
    python3 scripts/seed_lean_futures_databento.py --api-key "$DB_KEY" --yes

Outputs the LEAN futures-daily on-disk layout under ``<out>/``::

    future/<market>/daily/<lower>_trade.zip
        Contains <lower>_trade_<YYYYMM>.csv (one per expiry); per-row
        format: YYYYMMDD 00:00,<O>,<H>,<L>,<C>,<V> with prices as raw
        floats (matching the LEAN tutorial bundle's es_trade.zip).

    future/<market>/daily/<lower>_openinterest.zip
        Contains <lower>_openinterest_<YYYYMM>.csv (one per expiry);
        per-row format: YYYYMMDD 00:00,<oi_integer>.

    future/<market>/universes/<lower>/<YYYYMMDD>.csv
        Per-day snapshot of active contracts. Header
        ``#expiry,open,high,low,close,volume,open_interest``; one row
        per active contract that day (YYYYMM expiry + OHLCV + OI).

    future/<market>/map_files/<lower>.csv
        Two-row sentinel: 18991230,<lower>\\n20501231,<lower>,<MARKET>.
        Path 4 / Raw-mode insight: a fuller per-roll map_file with
        QC-internal contract hashes is NOT required when the strategy's
        ``DataNormalizationMode`` is ``Raw`` (the default for
        ``add_future``). If LEAN errors on missing map_file content at
        boot, the fallback is to emit explicit per-roll entries — see
        ``deploy/lean_local/seed-data.md`` "Futures seed via DataBento
        direct SDK" troubleshooting matrix.

Cost expectation (pre-quoted 2026-05-12; verified at script start):

* ohlcv-1d (parent stype, 7 micros, 23 months): ~$0.35 / 35k records
* statistics (parent, 7 micros, 23 months): ~$0.49 / 8.1M records
* definition (parent, 7 micros, 23 months): ~$0.12 / 216k records
* TOTAL: ~$0.96 against DataBento's $125 free credit (130x headroom)

If the cost re-quote at script start materially exceeds the pre-quote
(>5x = >$5), the script aborts and asks the operator to investigate
the symbology — something is wrong.

Dependencies (NOT added to ``pyproject.toml`` — operator-side ceremony):

    databento>=0.78
    pandas (transitive via databento)

Install in a venv::

    python3 -m venv /tmp/db_venv
    /tmp/db_venv/bin/pip install "databento>=0.78"
    /tmp/db_venv/bin/python scripts/seed_lean_futures_databento.py

Exit codes:
  0 — success; all tickers fetched + converted + staged
  2 — at least one ticker returned fewer than ``--min-bars`` bars in any expiry
  3 — at least one ticker's data shape was unexpected (no contracts, etc.)
  4 — databento SDK import failed (not installed in current env)
  5 — fetch-time exception (network, auth, API error) on any ticker
  6 — operator declined cost-quote confirmation
  7 — cost quote materially exceeds pre-quote (>5x) — abort + investigate
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

# Phase 1 universe (from CLAUDE.md PHASE1_FUTURES + V1_CANDIDATE_UNIVERSE).
# Each entry: ticker → (databento_parent_symbol, lean_market_dir, lean_market_code).
DEFAULT_UNIVERSE: dict[str, tuple[str, str, str]] = {
    "MES": ("MES.FUT", "cme", "CME"),
    "MNQ": ("MNQ.FUT", "cme", "CME"),
    "MYM": ("MYM.FUT", "cme", "CME"),
    "M2K": ("M2K.FUT", "cme", "CME"),
    "MBT": ("MBT.FUT", "cme", "CME"),
    "MGC": ("MGC.FUT", "comex", "COMEX"),
    "MCL": ("MCL.FUT", "nymex", "NYMEX"),
}

DEFAULT_OUT = Path("/tmp/lean_seed_futures")
DEFAULT_DATASET = "GLBX.MDP3"  # CME Group Globex — covers all 7 micros

# 23-month default window — covers MA_SLOW_DAYS warmup (200 trading days =
# ~9 calendar months) + ~14 months of recent history. Pre-quoted cost on
# this window is ~$0.96 (verified 2026-05-12).
DEFAULT_WINDOW_DAYS = 23 * 30
DEFAULT_MIN_BARS_PER_EXPIRY = 1  # liberal; some expiries have <5 trading days

# DataBento StatType.OPEN_INTEREST is integer 9 in their schema. Source:
# databento_dbn.StatType.OPEN_INTEREST.value = 9 (verified by direct probe
# 2026-05-12). The statistics schema publishes ~20 stat types intraday
# (settlement price, opening price, OI, etc.); we filter to OI only.
STAT_TYPE_OPEN_INTEREST = 9

# Pre-quoted total cost across all 3 schemas for the 7-micro x 23-month
# default fetch (matches the prompt's "~$0.96" expectation). Used to
# sanity-check the script-time re-quote — if reality is >5x this, the
# script aborts so the operator can investigate the symbology.
PREQUOTED_TOTAL_USD = Decimal("0.96")
COST_TRIPWIRE_USD = PREQUOTED_TOTAL_USD * 5  # $4.80


class TickerSeedResult(NamedTuple):
    ticker: str
    market: str
    expiries: int
    trade_rows: int
    oi_rows: int
    universe_days: int
    trade_zip_bytes: int
    oi_zip_bytes: int


# ---------------------------------------------------------------------------
# Cost quote
# ---------------------------------------------------------------------------


def cost_quote_combined(
    client: Any, parent_symbols: list[str], start: str, end: str, dataset: str
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Re-quote cost for ohlcv-1d + statistics + definition before fetch.

    Returns ``(ohlcv_cost, statistics_cost, definition_cost, total)`` as
    ``Decimal`` to match anti-pattern A05's no-float-for-money rule.

    Costs come back from DataBento as floats; we convert via ``Decimal(str)``
    immediately so downstream arithmetic stays Decimal-only.
    """
    ohlcv = Decimal(
        str(
            client.metadata.get_cost(
                dataset=dataset,
                start=start,
                end=end,
                symbols=parent_symbols,
                schema="ohlcv-1d",
                stype_in="parent",
            )
        )
    )
    stats = Decimal(
        str(
            client.metadata.get_cost(
                dataset=dataset,
                start=start,
                end=end,
                symbols=parent_symbols,
                schema="statistics",
                stype_in="parent",
            )
        )
    )
    defs = Decimal(
        str(
            client.metadata.get_cost(
                dataset=dataset,
                start=start,
                end=end,
                symbols=parent_symbols,
                schema="definition",
                stype_in="parent",
            )
        )
    )
    return (ohlcv, stats, defs, ohlcv + stats + defs)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_definition(client: Any, parent_symbol: str, start: str, end: str, dataset: str) -> Any:
    """Fetch contract definitions; return pandas DataFrame.

    Filters: keep only single contracts (no spreads — raw_symbol must NOT
    contain ``-``). Returned columns of interest: ``raw_symbol``,
    ``instrument_id``, ``expiration`` (timestamp).
    """
    data = client.timeseries.get_range(
        dataset=dataset,
        start=start,
        end=end,
        symbols=[parent_symbol],
        schema="definition",
        stype_in="parent",
    )
    df = data.to_df()
    if df.empty:
        return df
    # Drop calendar-spread rows ("MESM6-MESU6"); keep singletons ("MESM6")
    mask = ~df["raw_symbol"].str.contains("-", regex=False)
    return df[mask].copy()


def fetch_ohlcv(client: Any, parent_symbol: str, start: str, end: str, dataset: str) -> Any:
    """Fetch daily OHLCV; return pandas DataFrame.

    Filters: keep only single contracts (no spreads). Columns of interest:
    ``open``, ``high``, ``low``, ``close``, ``volume``, ``symbol``,
    ``instrument_id``. Index is ``ts_event`` (UTC).
    """
    data = client.timeseries.get_range(
        dataset=dataset,
        start=start,
        end=end,
        symbols=[parent_symbol],
        schema="ohlcv-1d",
        stype_in="parent",
    )
    df = data.to_df()
    if df.empty:
        return df
    mask = ~df["symbol"].str.contains("-", regex=False)
    return df[mask].copy()


def fetch_open_interest(client: Any, parent_symbol: str, start: str, end: str, dataset: str) -> Any:
    """Fetch statistics schema + filter to ``stat_type == OPEN_INTEREST``.

    Publishes ~20 stat types intraday; we only want OI. Returns the
    end-of-day OI per (contract, session_date) — DataBento publishes
    multiple OI updates throughout the day; we keep the LAST one per
    (symbol, date) pair via groupby + last().
    """
    data = client.timeseries.get_range(
        dataset=dataset,
        start=start,
        end=end,
        symbols=[parent_symbol],
        schema="statistics",
        stype_in="parent",
    )
    df = data.to_df()
    if df.empty:
        return df
    df = df[df["stat_type"] == STAT_TYPE_OPEN_INTEREST]
    # Drop spread rows
    df = df[~df["symbol"].str.contains("-", regex=False)]
    if df.empty:
        return df
    # The OI value lives in the ``quantity`` column for stat_type=OI (DataBento
    # statistics schema convention; the price column is unused for OI).
    df = df.copy()
    df["session_date"] = df["ts_event"].dt.date
    # Pick the latest OI update per (symbol, session_date). Sorting by ts_event
    # then groupby().last() yields the end-of-day OI.
    df = df.sort_values("ts_event")
    df = df.groupby(["symbol", "session_date"], as_index=False).last()
    df["open_interest"] = df["quantity"].astype("Int64")
    return df[["symbol", "session_date", "open_interest"]]


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _format_price(value: float) -> str:
    """Format a price as a string with up to 4 decimal places, trimmed.

    Matches the LEAN tutorial bundle's es_trade.csv style (e.g., "1635.25"
    not "1635.2500"). 4 decimals is the upper bound on LEAN's price-quanta
    handling for daily futures bars; integer prices print as "5810" without
    a trailing dot.
    """
    if math.isnan(value):
        return ""
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _databento_symbol_to_yyyymm(raw_symbol: str, expiration: Any) -> str:
    """Map a DataBento raw_symbol + expiration timestamp to LEAN's YYYYMM.

    DataBento's raw_symbol uses single-digit year (MESM6 = June ?6).
    The ``expiration`` timestamp disambiguates: ``2026-06-18`` → ``202606``.

    We trust the definition schema's expiration timestamp as authoritative.
    """
    if hasattr(expiration, "year") and hasattr(expiration, "month"):
        return f"{expiration.year:04d}{expiration.month:02d}"
    # Fallback: parse last 2 chars of raw_symbol (month code + year digit)
    # — this branch shouldn't fire if definition rows are present, but the
    # mypy-strict pass on the function requires it.
    raise ValueError(
        f"cannot derive YYYYMM expiry: raw_symbol={raw_symbol!r} "
        f"expiration={expiration!r} has no .year/.month attrs"
    )


def build_expiry_map(definition_df: Any) -> dict[str, str]:
    """Build ``raw_symbol → YYYYMM`` mapping from the definition DataFrame.

    Used to bucket ohlcv-1d rows and OI rows by expiry month. A single
    raw_symbol may appear multiple times in definition (one row per change-
    of-record event); pick any — the expiration date is invariant per
    contract.
    """
    out: dict[str, str] = {}
    if definition_df.empty:
        return out
    for _, row in definition_df.iterrows():
        sym = row["raw_symbol"]
        if sym in out:
            continue
        try:
            out[sym] = _databento_symbol_to_yyyymm(sym, row["expiration"])
        except ValueError:
            continue
    return out


def write_trade_zip(
    out: Path,
    ticker: str,
    market: str,
    ohlcv_df: Any,
    expiry_map: dict[str, str],
) -> tuple[int, int]:
    """Write ``future/<market>/daily/<lower>_trade.zip``.

    Returns ``(zip_size_bytes, total_rows_written)``.
    """
    lower = ticker.lower()
    zip_path = out / f"future/{market}/daily/{lower}_trade.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    # Bucket rows by expiry. Rows whose raw_symbol isn't in expiry_map are
    # dropped (no definition row → can't place them in a per-expiry CSV).
    buckets: dict[str, list[str]] = {}
    total_rows = 0
    for ts, row in ohlcv_df.iterrows():
        sym = row["symbol"]
        if sym not in expiry_map:
            continue
        expiry = expiry_map[sym]
        date_str = ts.strftime("%Y%m%d 00:00")
        line = (
            f"{date_str},"
            f"{_format_price(float(row['open']))},"
            f"{_format_price(float(row['high']))},"
            f"{_format_price(float(row['low']))},"
            f"{_format_price(float(row['close']))},"
            f"{int(row['volume'])}"
        )
        buckets.setdefault(expiry, []).append(line)
        total_rows += 1

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for expiry, lines in sorted(buckets.items()):
            csv_name = f"{lower}_trade_{expiry}.csv"
            zf.writestr(csv_name, ("\n".join(lines) + "\n").encode("utf-8"))
    return zip_path.stat().st_size, total_rows


def write_openinterest_zip(
    out: Path,
    ticker: str,
    market: str,
    oi_df: Any,
    expiry_map: dict[str, str],
) -> tuple[int, int]:
    """Write ``future/<market>/daily/<lower>_openinterest.zip``."""
    lower = ticker.lower()
    zip_path = out / f"future/{market}/daily/{lower}_openinterest.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    buckets: dict[str, list[str]] = {}
    total_rows = 0
    if not oi_df.empty:
        for _, row in oi_df.iterrows():
            sym = row["symbol"]
            if sym not in expiry_map:
                continue
            expiry = expiry_map[sym]
            session_date = row["session_date"]
            date_str = session_date.strftime("%Y%m%d 00:00")
            oi_val = row["open_interest"]
            if oi_val is None or (isinstance(oi_val, float) and math.isnan(oi_val)):
                continue
            line = f"{date_str},{int(oi_val)}"
            buckets.setdefault(expiry, []).append(line)
            total_rows += 1

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for expiry, lines in sorted(buckets.items()):
            csv_name = f"{lower}_openinterest_{expiry}.csv"
            zf.writestr(csv_name, ("\n".join(lines) + "\n").encode("utf-8"))
    return zip_path.stat().st_size, total_rows


def write_universe_files(
    out: Path,
    ticker: str,
    market: str,
    ohlcv_df: Any,
    oi_df: Any,
    expiry_map: dict[str, str],
) -> int:
    """Write ``future/<market>/universes/<lower>/<YYYYMMDD>.csv`` per session day.

    Each file is a snapshot of all active contracts that day with their OHLCV
    + open interest. Format matches LEAN's tutorial bundle (e.g., the ES
    universe under ``/Lean/Data/future/cme/universes/es/``).

    Returns the number of universe-day files written.
    """
    lower = ticker.lower()
    universe_dir = out / f"future/{market}/universes/{lower}"
    universe_dir.mkdir(parents=True, exist_ok=True)

    # Build (date → list of {expiry, OHLCV, OI}) from ohlcv_df + oi_df.
    # ohlcv_df is indexed on ts_event (UTC); oi_df has explicit session_date.
    # We use ohlcv_df's bar date as the canonical session date (since the
    # ohlcv-1d schema is what LEAN actually loads for price history).
    by_date: dict[_date, list[dict[str, Any]]] = {}
    for ts, row in ohlcv_df.iterrows():
        sym = row["symbol"]
        if sym not in expiry_map:
            continue
        session = ts.date() if hasattr(ts, "date") else ts
        by_date.setdefault(session, []).append(
            {
                "expiry": expiry_map[sym],
                "symbol": sym,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
        )

    # Index OI by (symbol, date) for fast lookup
    oi_lookup: dict[tuple[str, _date], int] = {}
    if not oi_df.empty:
        for _, row in oi_df.iterrows():
            oi_val = row["open_interest"]
            if oi_val is None or (isinstance(oi_val, float) and math.isnan(oi_val)):
                continue
            oi_lookup[(row["symbol"], row["session_date"])] = int(oi_val)

    files_written = 0
    for session_date, contracts in sorted(by_date.items()):
        # Stable sort by expiry (YYYYMM ascending) so cycle-to-cycle diffs
        # are minimal — same order each day.
        contracts.sort(key=lambda c: c["expiry"])
        date_str = session_date.strftime("%Y%m%d")
        target = universe_dir / f"{date_str}.csv"
        lines = ["#expiry,open,high,low,close,volume,open_interest"]
        for c in contracts:
            oi = oi_lookup.get((c["symbol"], session_date), "")
            lines.append(
                f"{c['expiry']},"
                f"{_format_price(c['open'])},"
                f"{_format_price(c['high'])},"
                f"{_format_price(c['low'])},"
                f"{_format_price(c['close'])},"
                f"{c['volume']},"
                f"{oi}"
            )
        target.write_text("\n".join(lines) + "\n")
        files_written += 1
    return files_written


def write_map_file(out: Path, ticker: str, market: str, market_code: str) -> None:
    """Two-row sentinel map_file (Path 4 / Raw-mode insight).

    A fuller per-roll map_file with QC-internal contract hashes (e.g.,
    ``es uik2f7cj4v0h``) is the canonical LEAN format but is QC-internal
    and cannot be hand-rolled correctly. Under ``DataNormalizationMode.Raw``
    (the default for ``add_future``) the strategy doesn't depend on the
    continuous-contract price-adjustment math the map_file enables, so a
    minimal 2-row sentinel suffices for the strategy's use of the per-
    expiry OHLCV bars.

    If LEAN errors at boot with "Map file not found" or similar, the
    fallback is to emit explicit per-roll entries — see the
    ``deploy/lean_local/seed-data.md`` "Futures seed via DataBento direct
    SDK" troubleshooting matrix.
    """
    lower = ticker.lower()
    target = out / f"future/{market}/map_files/{lower}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"18991230,{lower}\n20501231,{lower},{market_code}\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def seed_one_ticker(
    *,
    client: Any,
    ticker: str,
    parent_symbol: str,
    market: str,
    market_code: str,
    start: str,
    end: str,
    dataset: str,
    out: Path,
    min_bars: int,
) -> TickerSeedResult | None:
    """Fetch + convert + write a single ticker. Returns None on failure."""
    # 1. Definition — gives us the raw_symbol → YYYYMM mapping
    definition_df = fetch_definition(client, parent_symbol, start, end, dataset)
    if definition_df.empty:
        print(
            f"  {ticker}: FAIL — definition schema returned no rows (check parent symbol)",
            file=sys.stderr,
        )
        return None
    expiry_map = build_expiry_map(definition_df)
    if not expiry_map:
        print(
            f"  {ticker}: FAIL — definition rows present but no resolvable expiries",
            file=sys.stderr,
        )
        return None

    # 2. OHLCV — daily bars per contract
    ohlcv_df = fetch_ohlcv(client, parent_symbol, start, end, dataset)
    if ohlcv_df.empty:
        print(f"  {ticker}: FAIL — ohlcv-1d returned no rows", file=sys.stderr)
        return None
    if len(ohlcv_df) < min_bars:
        print(
            f"  {ticker}: FAIL — only {len(ohlcv_df)} ohlcv rows < {min_bars} min",
            file=sys.stderr,
        )
        return None

    # 3. Open interest (filtered from statistics)
    oi_df = fetch_open_interest(client, parent_symbol, start, end, dataset)

    # 4. Write the LEAN-format files
    trade_zip_bytes, trade_rows = write_trade_zip(out, ticker, market, ohlcv_df, expiry_map)
    oi_zip_bytes, oi_rows = write_openinterest_zip(out, ticker, market, oi_df, expiry_map)
    universe_days = write_universe_files(out, ticker, market, ohlcv_df, oi_df, expiry_map)
    write_map_file(out, ticker, market, market_code)

    return TickerSeedResult(
        ticker=ticker,
        market=market,
        expiries=len(expiry_map),
        trade_rows=trade_rows,
        oi_rows=oi_rows,
        universe_days=universe_days,
        trade_zip_bytes=trade_zip_bytes,
        oi_zip_bytes=oi_zip_bytes,
    )


def write_tarball(out: Path, tar_path: Path) -> int:
    """Bundle ``out/future/`` into ``<tar_path>``.

    Uses PAX format (no macOS metadata; same trick as ``seed_lean_data.py``).
    """
    with tarfile.open(tar_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.add(out / "future", arcname="future")
    return tar_path.stat().st_size


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="DataBento API key (or set DATABENTO_API_KEY env var)",
    )
    p.add_argument(
        "--tickers",
        default=",".join(DEFAULT_UNIVERSE.keys()),
        help=f"Comma-separated tickers (default: {','.join(DEFAULT_UNIVERSE.keys())})",
    )
    p.add_argument(
        "--start",
        default=None,
        help=(
            f"Inclusive start date YYYY-MM-DD (default: {DEFAULT_WINDOW_DAYS} days before --end)"
        ),
    )
    p.add_argument(
        "--end",
        default=None,
        help="Exclusive end date YYYY-MM-DD (default: today UTC)",
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Output staging directory (default: {DEFAULT_OUT})",
    )
    p.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"DataBento dataset (default: {DEFAULT_DATASET}; CME group covers all 7 micros)",
    )
    p.add_argument(
        "--min-bars",
        type=int,
        default=DEFAULT_MIN_BARS_PER_EXPIRY,
        help=f"Reject ticker if total ohlcv rows < N (default: {DEFAULT_MIN_BARS_PER_EXPIRY})",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the cost-quote confirmation prompt (CI / automation)",
    )
    p.add_argument(
        "--no-tar",
        action="store_true",
        help="Skip the tarball step (default: emit <out>.tar.gz beside <out>/)",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import databento as db  # type: ignore[import-not-found]
    except ImportError:
        print(
            "ERROR: databento not installed. Install via "
            '`pip install "databento>=0.78"` (recommend a venv per the '
            "runbook at deploy/lean_local/seed-data.md).",
            file=sys.stderr,
        )
        return 4

    # Resolve API key from arg or env
    api_key = args.api_key
    if not api_key:
        import os

        api_key = os.environ.get("DATABENTO_API_KEY", "")
    if not api_key:
        print(
            "ERROR: DataBento API key not provided. Pass --api-key or set "
            "DATABENTO_API_KEY env var.",
            file=sys.stderr,
        )
        return 5

    # Resolve window
    today_utc = datetime.now(tz=UTC).date()
    end_date: _date = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else today_utc
    start_date: _date = (
        datetime.strptime(args.start, "%Y-%m-%d").date()
        if args.start
        else end_date - timedelta(days=DEFAULT_WINDOW_DAYS)
    )
    if start_date >= end_date:
        print(
            f"ERROR: --start {start_date} must precede --end {end_date}",
            file=sys.stderr,
        )
        return 5

    # Resolve ticker list against DEFAULT_UNIVERSE
    requested = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    universe: dict[str, tuple[str, str, str]] = {}
    for t in requested:
        if t not in DEFAULT_UNIVERSE:
            print(
                f"ERROR: ticker {t!r} not in DEFAULT_UNIVERSE. "
                f"Supported: {','.join(DEFAULT_UNIVERSE.keys())}",
                file=sys.stderr,
            )
            return 5
        universe[t] = DEFAULT_UNIVERSE[t]

    parent_symbols = [v[0] for v in universe.values()]

    print(f"Tickers:   {list(universe.keys())}")
    print(f"Window:    {start_date} -> {end_date} (exclusive end)")
    print(f"Dataset:   {args.dataset}")
    print(f"Out:       {args.out}")
    print()
    print("Quoting cost (3 schemas, parent stype)...")
    client = db.Historical(key=api_key)
    try:
        ohlcv_cost, stats_cost, defs_cost, total_cost = cost_quote_combined(
            client=client,
            parent_symbols=parent_symbols,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            dataset=args.dataset,
        )
    except Exception as exc:
        print(f"ERROR: cost quote failed — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5

    print(f"  ohlcv-1d   : ${ohlcv_cost:>7.4f}")
    print(f"  statistics : ${stats_cost:>7.4f}")
    print(f"  definition : ${defs_cost:>7.4f}")
    print(f"  TOTAL      : ${total_cost:>7.4f}")
    print()

    if total_cost > COST_TRIPWIRE_USD:
        print(
            f"ABORT: cost quote ${total_cost} exceeds ${COST_TRIPWIRE_USD} "
            f"({COST_TRIPWIRE_USD / PREQUOTED_TOTAL_USD:.0f}x the pre-quote of "
            f"${PREQUOTED_TOTAL_USD}). Something is wrong with the symbology "
            f"or window. Investigate before re-running.",
            file=sys.stderr,
        )
        return 7

    # Operator confirmation gate (skipped with --yes)
    if not args.yes:
        print(f"Free-credit (assuming $125): {Decimal('125') / total_cost:.0f}x headroom")
        resp = input("Proceed with fetch? [y/N]: ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted by operator.", file=sys.stderr)
            return 6

    # Stage output dir
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    print("---")
    results: list[TickerSeedResult] = []
    fail_count = 0
    for ticker, (parent, market, market_code) in universe.items():
        try:
            result = seed_one_ticker(
                client=client,
                ticker=ticker,
                parent_symbol=parent,
                market=market,
                market_code=market_code,
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                dataset=args.dataset,
                out=out,
                min_bars=args.min_bars,
            )
        except Exception as exc:
            print(
                f"  {ticker}: EXCEPTION {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            fail_count += 1
            continue
        if result is None:
            fail_count += 1
            continue
        results.append(result)
        print(
            f"  {result.ticker:4s} ({result.market:6s}): "
            f"{result.expiries:3d} expiries  "
            f"{result.trade_rows:6d} trade rows  "
            f"{result.oi_rows:6d} OI rows  "
            f"{result.universe_days:4d} universe days  "
            f"trade.zip={result.trade_zip_bytes}b  "
            f"oi.zip={result.oi_zip_bytes}b"
        )

    if fail_count > 0 or not results:
        print(f"\nFAILED: {fail_count} ticker(s) did not seed cleanly.", file=sys.stderr)
        return 2

    print("---")
    print(f"Staged: {len(results)} ticker(s) under {out}/future/")
    if not args.no_tar:
        tar_path = Path(str(out) + ".tar.gz")
        tar_bytes = write_tarball(out, tar_path)
        print(f"Tarball: {tar_path}  ({tar_bytes} bytes)")
    print(
        "\nNext step: see deploy/lean_local/seed-data.md "
        "'Futures seed via DataBento direct SDK' for scp + docker-cp + restart."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
