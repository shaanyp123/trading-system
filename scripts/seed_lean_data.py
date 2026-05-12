"""Build a LEAN-format equity-daily seed bundle for the Phase 1 ETF universe.

Canonicalized from the 2026-05-12 seed ceremony (Docs/decisions-log.md entry
"Phase 1 data-seed ceremony — ETF universe seeded to `trading_lean_data`").

Usage examples::

    # Default: TLT/IEF/SHY/TIP, ~360 trading days back from yesterday, stage
    # to /tmp/lean_seed/staged/, also emit /tmp/lean_seed/staged.tar.gz.
    python3 scripts/seed_lean_data.py

    # Custom window + custom tickers + custom output dir + skip tar:
    python3 scripts/seed_lean_data.py \
        --tickers TLT,IEF,SHY,TIP,LQD,SPY \
        --start 2024-01-01 \
        --end 2026-05-12 \
        --out /tmp/lean_seed_v2 \
        --no-tar

Outputs the LEAN equity-daily on-disk layout under <out>/::

    equity/usa/daily/<lower>.zip       containing <lower>.csv with rows
                                       YYYYMMDD 00:00,O*10000,H*10000,L*10000,C*10000,V
    equity/usa/map_files/<lower>.csv   2-row: <date>,<lower>,<exchange-code>
    equity/usa/factor_files/<lower>.csv  2-row: <date>,1,1,<ref-price>

Then operator runs the docker cp + restart steps per
``deploy/lean_local/seed-data.md`` to land the bundle in the
``trading_lean_data`` Docker volume on the VPS.

Important format conventions (LEAN equity-daily):

* Prices in the CSV are stored as **integers scaled by 10000** (deci-cents).
  A close of $85.56 round-trips as ``855600``.
* Timestamps are ``YYYYMMDD 00:00`` — daily bars use midnight ET as the bar
  start; LEAN's ``TradeBar.end_time`` is the close-of-bar timestamp computed
  by the loader.
* Volume is raw integer shares (not scaled).
* Exchange codes (column 3 of map_files): ``P`` = NYSE Arca, ``Q`` = Nasdaq,
  ``N`` = NYSE. Override per ticker via ``--exchange``.

Dependencies: ``yfinance`` (free public daily bars). Install in a venv::

    python3 -m venv /tmp/lean_seed_venv
    /tmp/lean_seed_venv/bin/pip install yfinance
    /tmp/lean_seed_venv/bin/python scripts/seed_lean_data.py

The script intentionally does NOT add ``yfinance`` to ``pyproject.toml`` — it
is a one-off operator-side ceremony script, not a runtime dependency of any
service container. Keeping ``yfinance`` out of pyproject keeps the
api/discord_bot/webhook_pusher images lean and avoids the dep-drift CI gate.

Exit codes:
  0 — success; all tickers fetched + staged
  2 — at least one ticker returned fewer than ``--min-bars`` rows
  3 — at least one ticker's last close was non-positive
  4 — yfinance import failed (not installed in current Python env)
  5 — at least one ticker raised a fetch-time exception
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
from pathlib import Path
from typing import Any, NamedTuple

DEFAULT_TICKERS = ("TLT", "IEF", "SHY", "TIP")
DEFAULT_EXCHANGE = "P"  # NYSE Arca — covers all 4 Phase 1 bond ETFs
DEFAULT_OUT = Path("/tmp/lean_seed/staged")

# Strategy-side warmup window: MA_SLOW_DAYS (200) + ATR_LOOKBACK_DAYS (20) +
# 5-day pad = 225 trading days. We fetch ~360 calendar-day window by default
# which yields ~250 trading days — comfortably above the warmup minimum.
DEFAULT_WINDOW_DAYS = 360
DEFAULT_MIN_BARS = 225


class SeedResult(NamedTuple):
    ticker: str
    rows: int
    first_date: str
    last_date: str
    last_close: float
    zip_bytes: int


def _isna(x: object) -> bool:
    """NaN-safe check that survives non-numeric inputs (e.g. None, strings)."""
    try:
        return math.isnan(float(x))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def fetch_one(ticker: str, start: str, end: str) -> Any:
    """Per-ticker fetch (avoids yfinance's multi-ticker SQLite cache lock)."""
    import yfinance as yf  # type: ignore[import-not-found]  # local import → clean exit-4 if missing

    return yf.Ticker(ticker).history(
        start=start, end=end, interval="1d", auto_adjust=False, actions=False
    )


def to_lean_csv(df: Any, ticker: str) -> bytes:
    """Convert a yfinance OHLCV DataFrame → LEAN equity-daily CSV bytes."""
    lines: list[str] = []
    for ts, row in df.iterrows():
        o = float(row["Open"])
        h = float(row["High"])
        low = float(row["Low"])
        c = float(row["Close"])
        raw_v = row["Volume"]
        v = int(raw_v) if not _isna(raw_v) else 0
        if any(_isna(x) for x in (o, h, low, c)):
            continue
        if c <= 0:
            continue
        date_str = ts.strftime("%Y%m%d 00:00")
        lines.append(
            f"{date_str},{round(o * 10000)},{round(h * 10000)},"
            f"{round(low * 10000)},{round(c * 10000)},{v}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_map_file(out: Path, ticker: str, exchange: str) -> None:
    """LEAN map_files: 2-row <date>,<lower>,<exchange-code>."""
    lower = ticker.lower()
    target = out / f"equity/usa/map_files/{lower}.csv"
    target.write_text(f"19980102,{lower},{exchange}\n20501231,{lower},{exchange}\n")


def write_factor_file(out: Path, ticker: str, ref_price: float) -> None:
    """LEAN factor_files: 2-row, no dividend/split adjustments.

    We use ``price_factor = 1, split_factor = 1`` because the strategy works
    on raw closes (Donchian/MA/Hurst/ATR on un-adjusted price). The
    reference_price in the first row uses the most recent close; the
    sentinel last row uses 0 per LEAN convention.
    """
    lower = ticker.lower()
    target = out / f"equity/usa/factor_files/{lower}.csv"
    target.write_text(f"19980102,1,1,{ref_price:.4f}\n20501231,1,1,0\n")


def write_daily_zip(out: Path, ticker: str, csv_bytes: bytes) -> int:
    """Write equity/usa/daily/<lower>.zip; return bytes written."""
    lower = ticker.lower()
    zip_path = out / f"equity/usa/daily/{lower}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{lower}.csv", csv_bytes)
    return zip_path.stat().st_size


def stage_one(
    ticker: str, start: str, end: str, min_bars: int, out: Path, exchange: str
) -> SeedResult | None:
    """Fetch + validate + write a single ticker. Returns None on failure."""
    df = fetch_one(ticker, start=start, end=end)
    rows = len(df)
    if rows < min_bars:
        print(f"  {ticker}: FAIL — only {rows} rows < {min_bars}", file=sys.stderr)
        return None
    last_close = float(df.iloc[-1]["Close"])
    if last_close <= 0:
        print(f"  {ticker}: FAIL — last_close {last_close} <= 0", file=sys.stderr)
        return None
    csv_bytes = to_lean_csv(df, ticker)
    surviving_rows = csv_bytes.count(b"\n")
    if surviving_rows < min_bars:
        print(
            f"  {ticker}: FAIL — after NaN/non-positive-close filtering, "
            f"only {surviving_rows} rows survived (< {min_bars})",
            file=sys.stderr,
        )
        return None
    zip_bytes = write_daily_zip(out, ticker, csv_bytes)
    write_map_file(out, ticker, exchange)
    write_factor_file(out, ticker, last_close)
    return SeedResult(
        ticker=ticker,
        rows=rows,
        first_date=df.index[0].strftime("%Y-%m-%d"),
        last_date=df.index[-1].strftime("%Y-%m-%d"),
        last_close=last_close,
        zip_bytes=zip_bytes,
    )


def write_tarball(out: Path, tar_path: Path) -> int:
    """Bundle the staging dir into a single tarball for scp.

    Uses ``--no-mac-metadata`` semantics (PAX format without ``._<name>``
    AppleDouble entries) so the bundle is clean across macOS / Linux. The
    2026-05-12 ceremony hit this trap when a naive ``tar -czf`` on macOS
    leaked ``._file`` sidecars into the docker volume.
    """
    with tarfile.open(tar_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.add(out / "equity", arcname="equity")
    return tar_path.stat().st_size


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help=f"Comma-separated ticker list (default: {','.join(DEFAULT_TICKERS)})",
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
        help=(
            "Exclusive end date YYYY-MM-DD "
            "(default: today UTC; yfinance treats this as exclusive so the "
            "last bar is the previous trading day's finalized close)"
        ),
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Output staging directory (default: {DEFAULT_OUT})",
    )
    p.add_argument(
        "--exchange",
        default=DEFAULT_EXCHANGE,
        choices=("P", "Q", "N"),
        help=(
            "LEAN exchange code applied to every ticker's map_files entry "
            "(P=NYSE Arca, Q=Nasdaq, N=NYSE; default: P for bond ETFs)"
        ),
    )
    p.add_argument(
        "--min-bars",
        type=int,
        default=DEFAULT_MIN_BARS,
        help=(
            f"Reject the build if any ticker returns < N rows "
            f"(default: {DEFAULT_MIN_BARS} = V1 warmup minimum)"
        ),
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
        import yfinance  # noqa: F401
    except ImportError:
        print(
            "ERROR: yfinance not installed in this Python env. Install via "
            "`pip install yfinance` (recommend a venv per the runbook at "
            "deploy/lean_local/seed-data.md).",
            file=sys.stderr,
        )
        return 4

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

    tickers = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if not tickers:
        print("ERROR: --tickers resolved to empty list", file=sys.stderr)
        return 5

    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    (out / "equity/usa/daily").mkdir(parents=True, exist_ok=True)
    (out / "equity/usa/map_files").mkdir(parents=True, exist_ok=True)
    (out / "equity/usa/factor_files").mkdir(parents=True, exist_ok=True)

    print(f"Tickers:   {list(tickers)}")
    print(f"Window:    {start_date} -> {end_date} (exclusive end)")
    print(f"Exchange:  {args.exchange} (applied to all tickers)")
    print(f"Min bars:  {args.min_bars}")
    print(f"Out:       {out}")
    print("---")

    results: list[SeedResult] = []
    fail_count = 0
    for ticker in tickers:
        try:
            result = stage_one(
                ticker=ticker,
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                min_bars=args.min_bars,
                out=out,
                exchange=args.exchange,
            )
        except Exception as exc:
            print(f"  {ticker}: EXCEPTION {type(exc).__name__}: {exc}", file=sys.stderr)
            fail_count += 1
            continue
        if result is None:
            fail_count += 1
            continue
        results.append(result)
        print(
            f"  {result.ticker}: {result.rows} rows  "
            f"{result.first_date}..{result.last_date}  "
            f"last_close=${result.last_close:.2f}  zip={result.zip_bytes}b"
        )

    if fail_count > 0 or not results:
        print(f"\nFAILED: {fail_count} ticker(s) did not stage cleanly.", file=sys.stderr)
        return 2

    print("---")
    print(f"Staged: {len(results)} ticker(s) under {out}/equity/usa/")
    if not args.no_tar:
        tar_path = Path(str(out) + ".tar.gz")
        tar_bytes = write_tarball(out, tar_path)
        print(f"Tarball: {tar_path}  ({tar_bytes} bytes)")
    print("\nNext step: see deploy/lean_local/seed-data.md for scp + docker-cp + restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
