"""Cross-check seeded LEAN equity-daily bars against an independent free data source.

Sibling to ``scripts/seed_lean_data.py``. Where ``seed_lean_data.py`` fetches
daily OHLCV from Yahoo Finance via ``yfinance`` and writes the LEAN-format
on-disk bundle, this script reads what's ALREADY on disk in the
``trading_lean_data`` Docker volume (LEAN equity-daily ``<lower>.zip`` files)
and compares the close prices against a second independent provider.

The point: catch a "garbage-in-garbage-out" failure mode (yfinance data
quality regression, accidental wrong-window seed, decoder bug, etc.) BEFORE
the operator approves any live signal based on the bar data.

Usage examples::

    # Default — TLT/IEF/SHY/TIP vs Stooq (no API key needed):
    python3 scripts/verify_seed_data.py --data-dir /Lean/Data

    # Single ticker vs Tiingo (free tier, signup gets you the key):
    python3 scripts/verify_seed_data.py --data-dir /Lean/Data \\
        --tickers TLT --source tiingo --tiingo-api-key "$TIINGO_API_KEY"

    # Tighter threshold + emit JSON for downstream:
    python3 scripts/verify_seed_data.py --data-dir /Lean/Data \\
        --threshold-bp 0.5 --json --out /tmp/seed-verify.json

Inputs (read-only against ``--data-dir``)::

    <data-dir>/equity/usa/daily/<lower>.zip   containing <lower>.csv with rows
    YYYYMMDD 00:00,O*10000,H*10000,L*10000,C*10000,V

Output for each ticker:

* Row count on each side + shared-date count + dates-only-in-one-side count
* Max divergence (bp) on any shared date
* Count of shared dates with divergence > ``--threshold-bp``
* Up to ``--show-worst N`` worst-offender rows for human eyeballing

Exit codes:
  0 — every ticker's shared-date set is within ``--threshold-bp``
  1 — at least one shared date exceeded ``--threshold-bp`` on any ticker
  2 — fetch failed for at least one ticker (HTTP error / parse error /
       cross-source returned no rows)

Dependencies: stdlib only. No ``yfinance``, ``requests``, ``pandas`` — the
script is portable enough to drop straight into a minimal ``python:3.11-slim``
container with the volume mounted (see ``deploy/lean_local/seed-data.md``
"cross-check" runbook section).

Cross-source choices:

* ``stooq`` (default) — Polish financial-data site; free public CSV endpoint
  at ``https://stooq.com/q/d/l/?s=<ticker>.us&d1=YYYYMMDD&d2=YYYYMMDD&i=d``;
  no API key. Independent of Yahoo. Schema: ``Date,Open,High,Low,Close,Volume``.
* ``tiingo`` — Free tier (1000 req/day) with signup. Independent of Yahoo
  AND Stooq. Schema: full OHLCV with adjusted columns. Endpoint:
  ``https://api.tiingo.com/tiingo/daily/<ticker>/prices?startDate=...
  &endDate=...&format=csv&token=<api_key>``.

Float arithmetic is intentionally avoided per anti-pattern [A05]: all close
prices flow through ``decimal.Decimal`` end-to-end (LEAN deci-cent integers
parsed via ``Decimal(int_str) / Decimal(10000)``; cross-source CSV strings
parsed via ``Decimal(str)``).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Literal

# Callable type alias used by the fetch_* signatures to keep mypy --strict
# happy without leaking ``Any``.
HttpGet = Callable[[str], str]

DEFAULT_TICKERS = ("TLT", "IEF", "SHY", "TIP")
DEFAULT_DATA_DIR = Path("/Lean/Data")

# 1bp = 0.01% = 0.0001 in price-ratio terms. The trading system's strategy
# logic operates on close prices to 4-decimal precision (raw equity dollars +
# 4 decimals); a 1bp divergence on an $85 ETF is ~$0.0085. This threshold is
# chosen for "would a Donchian breakout fire on a different bar?" — well
# below the strategy's signal-relevance noise floor.
DEFAULT_THRESHOLD_BP = Decimal("1.0")

# Cap how many cross-source dates we ask for at once. Stooq's public endpoint
# happily returns a year+ of daily bars in one CSV; Tiingo's daily endpoint
# has no documented per-request limit on the free tier for daily-OHLCV.
DEFAULT_USER_AGENT = "trading-system-verify-seed-data/1.0 (+ops)"

# Stooq + Tiingo endpoints are committed as constants so a future endpoint
# change is a single-line diff, and so the tests can monkeypatch them.
STOOQ_URL_TEMPLATE = "https://stooq.com/q/d/l/?s={ticker_lower}.us&d1={d1}&d2={d2}&i=d"
TIINGO_URL_TEMPLATE = (
    "https://api.tiingo.com/tiingo/daily/{ticker_lower}/prices"
    "?startDate={start}&endDate={end}&format=csv&token={token}"
)

SourceName = Literal["stooq", "tiingo"]


@dataclass(frozen=True)
class TickerDiffRow:
    """Per-date diff between LEAN seed close and cross-source close."""

    date_iso: str  # YYYY-MM-DD
    lean_close: Decimal
    cross_close: Decimal
    divergence_bp: Decimal  # |lean - cross| / cross * 10000

    @property
    def signed_divergence_bp(self) -> Decimal:
        """Signed divergence: positive means LEAN > cross-source."""
        if self.cross_close == 0:
            return Decimal("0")
        return (self.lean_close - self.cross_close) / self.cross_close * Decimal("10000")


@dataclass(frozen=True)
class TickerVerification:
    """Per-ticker verification outcome."""

    ticker: str
    lean_rows: int
    cross_rows: int
    shared_dates: int
    dates_only_in_lean: int
    dates_only_in_cross: int
    max_divergence_bp: Decimal
    rows_over_threshold: int
    worst_rows: tuple[TickerDiffRow, ...]
    fetch_error: str | None = None

    @property
    def passed(self) -> bool:
        return self.fetch_error is None and self.rows_over_threshold == 0


def read_lean_zip(zip_path: Path) -> dict[str, Decimal]:
    """Decode the LEAN equity-daily zip → dict[YYYY-MM-DD] = close_decimal.

    LEAN's on-disk format is ``<lower>.zip`` containing ``<lower>.csv`` with
    rows ``YYYYMMDD 00:00,O*10000,H*10000,L*10000,C*10000,V``. We only care
    about the close (5th column) here. Prices are stored as integers scaled
    by 10000 (deci-cents); divide by 10000 to recover dollars.
    """
    if not zip_path.is_file():
        raise FileNotFoundError(f"LEAN zip not found: {zip_path}")
    out: dict[str, Decimal] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if len(names) != 1:
            raise ValueError(
                f"LEAN zip {zip_path} contains {len(names)} entries; expected exactly 1 CSV"
            )
        csv_name = names[0]
        with zf.open(csv_name, "r") as fp:
            text = fp.read().decode("utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 6:
            raise ValueError(
                f"LEAN row {line_no} in {zip_path}: expected 6 fields, got {len(parts)}: {line!r}"
            )
        ts_token = parts[0]
        close_token = parts[4]
        if " " in ts_token:
            ymd = ts_token.split(" ", 1)[0]
        else:
            ymd = ts_token
        if len(ymd) != 8 or not ymd.isdigit():
            raise ValueError(f"LEAN row {line_no} in {zip_path}: bad date token {ts_token!r}")
        iso = f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
        close_scaled = Decimal(close_token)
        close = close_scaled / Decimal("10000")
        out[iso] = close
    return out


def _http_get(url: str, *, timeout: float = 30.0) -> str:
    """stdlib HTTP GET with a polite UA + UTF-8 decode.

    Wraps urllib for the same effect as ``requests.get(...).text`` without
    adding a dep. Returns the response body as a string. Raises
    ``urllib.error.URLError``/``HTTPError`` on transport/HTTP failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body: bytes = resp.read()
    return body.decode("utf-8", errors="replace")


def parse_stooq_csv(body: str) -> dict[str, Decimal]:
    """Parse Stooq's daily-CSV response → dict[YYYY-MM-DD] = close_decimal.

    Header: ``Date,Open,High,Low,Close,Volume`` (no quoting, comma-separated).
    Dates are already ``YYYY-MM-DD`` ISO. Stooq emits a literal ``"No data"``
    body when a ticker is unknown or the window has no data — we surface
    that as a parser-level error (treated as fetch failure → exit 2).
    """
    stripped = body.strip()
    if not stripped or stripped.lower().startswith("no data"):
        raise ValueError("stooq returned empty / 'no data' body")
    reader = csv.DictReader(io.StringIO(stripped))
    if (
        reader.fieldnames is None
        or "Date" not in reader.fieldnames
        or "Close" not in reader.fieldnames
    ):
        raise ValueError(f"stooq CSV header missing Date/Close columns: {reader.fieldnames!r}")
    out: dict[str, Decimal] = {}
    for row in reader:
        date_iso = (row.get("Date") or "").strip()
        close_str = (row.get("Close") or "").strip()
        if not date_iso or not close_str:
            continue
        out[date_iso] = Decimal(close_str)
    if not out:
        raise ValueError("stooq CSV parsed to zero rows (header-only body?)")
    return out


def parse_tiingo_csv(body: str) -> dict[str, Decimal]:
    """Parse Tiingo's daily-OHLCV CSV response → dict[YYYY-MM-DD] = close_decimal.

    Header (free tier, daily endpoint): ``date,close,high,low,open,volume,
    adjClose,adjHigh,adjLow,adjOpen,adjVolume,divCash,splitFactor``.
    The leading ``date`` is ``YYYY-MM-DDTHH:MM:SS.000Z``; we strip the time.

    We deliberately compare against the RAW (un-adjusted) ``close`` column to
    match the LEAN-on-disk seed convention (``scripts/seed_lean_data.py``
    writes ``factor_files`` with ``1,1`` — no dividend/split adjustments —
    so the on-disk close is also un-adjusted).
    """
    stripped = body.strip()
    if not stripped:
        raise ValueError("tiingo returned empty body")
    reader = csv.DictReader(io.StringIO(stripped))
    fields = reader.fieldnames or []
    if "date" not in fields or "close" not in fields:
        raise ValueError(f"tiingo CSV header missing date/close columns: {fields!r}")
    out: dict[str, Decimal] = {}
    for row in reader:
        date_raw = (row.get("date") or "").strip()
        close_str = (row.get("close") or "").strip()
        if not date_raw or not close_str:
            continue
        if "T" in date_raw:
            date_iso = date_raw.split("T", 1)[0]
        else:
            date_iso = date_raw[:10]
        out[date_iso] = Decimal(close_str)
    if not out:
        raise ValueError("tiingo CSV parsed to zero rows (header-only body?)")
    return out


def fetch_stooq(
    ticker: str,
    start: _date,
    end: _date,
    *,
    http_get: HttpGet | None = None,
) -> dict[str, Decimal]:
    """Fetch daily closes from Stooq for ``ticker.us`` over ``[start, end]``.

    Note Stooq's window is inclusive on both ends (unlike yfinance's
    exclusive ``end``). ``http_get`` is injectable for tests.
    """
    url = STOOQ_URL_TEMPLATE.format(
        ticker_lower=ticker.lower(),
        d1=start.strftime("%Y%m%d"),
        d2=end.strftime("%Y%m%d"),
    )
    body = (http_get or _http_get)(url)
    return parse_stooq_csv(body)


def fetch_tiingo(
    ticker: str,
    start: _date,
    end: _date,
    *,
    api_key: str,
    http_get: HttpGet | None = None,
) -> dict[str, Decimal]:
    """Fetch daily closes from Tiingo for ``ticker`` over ``[start, end]``.

    ``http_get`` is injectable for tests; the real path uses urllib.
    """
    if not api_key:
        raise ValueError("tiingo: --tiingo-api-key required (or set TIINGO_API_KEY env var)")
    url = TIINGO_URL_TEMPLATE.format(
        ticker_lower=ticker.lower(),
        start=start.isoformat(),
        end=end.isoformat(),
        token=api_key,
    )
    body = (http_get or _http_get)(url)
    return parse_tiingo_csv(body)


def compute_diff(
    *,
    ticker: str,
    lean_data: dict[str, Decimal],
    cross_data: dict[str, Decimal],
    threshold_bp: Decimal,
    show_worst: int,
) -> TickerVerification:
    """Compute the per-date diff and aggregate per-ticker summary.

    Divergence formula: ``|lean - cross| / cross * 10000`` (basis points).
    A row breaches the threshold iff its divergence > ``threshold_bp``.
    The worst rows are sorted by descending absolute divergence.
    """
    lean_dates = set(lean_data.keys())
    cross_dates = set(cross_data.keys())
    shared = lean_dates & cross_dates
    only_lean = lean_dates - cross_dates
    only_cross = cross_dates - lean_dates

    rows: list[TickerDiffRow] = []
    for date_iso in sorted(shared):
        lean_close = lean_data[date_iso]
        cross_close = cross_data[date_iso]
        if cross_close == 0:
            # Defensive: divide-by-zero in divergence-bp would crash the
            # Decimal arithmetic. Treat as worst-case (effectively infinite
            # divergence) — surface it in the worst-rows list with a
            # sentinel divergence > threshold so the operator sees it.
            divergence = Decimal("99999999")
        else:
            divergence = abs(lean_close - cross_close) / cross_close * Decimal("10000")
        rows.append(
            TickerDiffRow(
                date_iso=date_iso,
                lean_close=lean_close,
                cross_close=cross_close,
                divergence_bp=divergence,
            )
        )

    rows_sorted_by_divergence = sorted(rows, key=lambda r: r.divergence_bp, reverse=True)
    worst_rows = tuple(rows_sorted_by_divergence[: max(show_worst, 0)])
    max_divergence_bp = (
        rows_sorted_by_divergence[0].divergence_bp if rows_sorted_by_divergence else Decimal("0")
    )
    rows_over_threshold = sum(1 for r in rows if r.divergence_bp > threshold_bp)

    return TickerVerification(
        ticker=ticker,
        lean_rows=len(lean_data),
        cross_rows=len(cross_data),
        shared_dates=len(shared),
        dates_only_in_lean=len(only_lean),
        dates_only_in_cross=len(only_cross),
        max_divergence_bp=max_divergence_bp,
        rows_over_threshold=rows_over_threshold,
        worst_rows=worst_rows,
    )


def _format_bp(value: Decimal) -> str:
    """Format a basis-points Decimal as ``N.NNbp`` for the text table."""
    return f"{value:.4f}bp"


def _format_price(value: Decimal) -> str:
    """Format a price Decimal as ``$N.NNNN`` (4-decimal precision)."""
    return f"${value:.4f}"


def _print_text_summary(
    *,
    source: SourceName,
    data_dir: Path,
    start: _date,
    end: _date,
    threshold_bp: Decimal,
    verifications: tuple[TickerVerification, ...],
) -> None:
    print(f"\nLEAN seed verification vs '{source}'")
    print(f"Data dir:   {data_dir}")
    print(f"Window:     {start.isoformat()} -> {end.isoformat()} (inclusive on both ends)")
    print(f"Threshold:  {_format_bp(threshold_bp)} on close-price divergence\n")
    header = (
        f"{'Ticker':<8}  {'LEAN rows':>10}  {'Cross rows':>11}  "
        f"{'Shared':>7}  {'Only LEAN':>10}  {'Only cross':>11}  "
        f"{'Max bp':>10}  {'Breaches':>9}  Result"
    )
    print(header)
    print("-" * len(header))
    for v in verifications:
        if v.fetch_error is not None:
            print(f"{v.ticker:<8}  FETCH FAILED — {v.fetch_error}")
            continue
        result = "PASS" if v.passed else "FAIL"
        print(
            f"{v.ticker:<8}  {v.lean_rows:>10}  {v.cross_rows:>11}  "
            f"{v.shared_dates:>7}  {v.dates_only_in_lean:>10}  {v.dates_only_in_cross:>11}  "
            f"{_format_bp(v.max_divergence_bp):>10}  {v.rows_over_threshold:>9}  {result}"
        )
    print()
    any_worst = any(v.worst_rows for v in verifications if v.fetch_error is None)
    if any_worst:
        print("Worst-divergence rows per ticker (descending by abs(bp)):")
        for v in verifications:
            if v.fetch_error is not None or not v.worst_rows:
                continue
            print(f"\n  {v.ticker}:")
            sub_header = f"    {'Date':<12}  {'LEAN close':>12}  {'Cross close':>13}  {'abs bp':>10}  {'signed bp':>12}"
            print(sub_header)
            print(f"    {'-' * (len(sub_header) - 4)}")
            for r in v.worst_rows:
                print(
                    f"    {r.date_iso:<12}  {_format_price(r.lean_close):>12}  "
                    f"{_format_price(r.cross_close):>13}  "
                    f"{_format_bp(r.divergence_bp):>10}  {_format_bp(r.signed_divergence_bp):>12}"
                )
        print()


def _build_json_payload(
    *,
    source: SourceName,
    data_dir: Path,
    start: _date,
    end: _date,
    threshold_bp: Decimal,
    verifications: tuple[TickerVerification, ...],
) -> dict[str, object]:
    """Build a machine-readable payload for --json / --out."""
    return {
        "source": source,
        "data_dir": str(data_dir),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "threshold_bp": str(threshold_bp),
        "generated_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "tickers": [
            {
                "ticker": v.ticker,
                "passed": v.passed,
                "fetch_error": v.fetch_error,
                "lean_rows": v.lean_rows,
                "cross_rows": v.cross_rows,
                "shared_dates": v.shared_dates,
                "dates_only_in_lean": v.dates_only_in_lean,
                "dates_only_in_cross": v.dates_only_in_cross,
                "max_divergence_bp": str(v.max_divergence_bp),
                "rows_over_threshold": v.rows_over_threshold,
                "worst_rows": [
                    {
                        "date_iso": r.date_iso,
                        "lean_close": str(r.lean_close),
                        "cross_close": str(r.cross_close),
                        "divergence_bp": str(r.divergence_bp),
                        "signed_divergence_bp": str(r.signed_divergence_bp),
                    }
                    for r in v.worst_rows
                ],
            }
            for v in verifications
        ],
    }


def _resolve_window_from_lean(
    *,
    explicit_start: _date | None,
    explicit_end: _date | None,
    lean_data: dict[str, Decimal],
) -> tuple[_date, _date]:
    """Pick the cross-source query window from explicit args or LEAN coverage.

    If the user passed ``--start`` / ``--end``, use those. Otherwise default
    to ``[min(lean_dates), max(lean_dates)]`` so the cross-source query
    exactly matches what's on disk.
    """
    if explicit_start is not None and explicit_end is not None:
        return explicit_start, explicit_end
    if not lean_data:
        raise ValueError("cannot derive window from empty LEAN data")
    dates_iso = sorted(lean_data.keys())
    derived_start = datetime.strptime(dates_iso[0], "%Y-%m-%d").date()
    derived_end = datetime.strptime(dates_iso[-1], "%Y-%m-%d").date()
    return explicit_start or derived_start, explicit_end or derived_end


def verify_one(
    *,
    ticker: str,
    data_dir: Path,
    source: SourceName,
    tiingo_api_key: str | None,
    explicit_start: _date | None,
    explicit_end: _date | None,
    threshold_bp: Decimal,
    show_worst: int,
    http_get: HttpGet | None = None,
) -> TickerVerification:
    """Verify a single ticker: read LEAN, fetch cross-source, diff."""
    zip_path = data_dir / "equity" / "usa" / "daily" / f"{ticker.lower()}.zip"
    try:
        lean_data = read_lean_zip(zip_path)
    except (FileNotFoundError, ValueError, zipfile.BadZipFile) as exc:
        return TickerVerification(
            ticker=ticker,
            lean_rows=0,
            cross_rows=0,
            shared_dates=0,
            dates_only_in_lean=0,
            dates_only_in_cross=0,
            max_divergence_bp=Decimal("0"),
            rows_over_threshold=0,
            worst_rows=(),
            fetch_error=f"LEAN read failed: {type(exc).__name__}: {exc}",
        )

    start, end = _resolve_window_from_lean(
        explicit_start=explicit_start,
        explicit_end=explicit_end,
        lean_data=lean_data,
    )

    try:
        if source == "stooq":
            cross_data = fetch_stooq(ticker, start, end, http_get=http_get)
        else:  # source == "tiingo" (Literal narrows here)
            cross_data = fetch_tiingo(
                ticker,
                start,
                end,
                api_key=tiingo_api_key or "",
                http_get=http_get,
            )
    except (urllib.error.URLError, ValueError) as exc:
        return TickerVerification(
            ticker=ticker,
            lean_rows=len(lean_data),
            cross_rows=0,
            shared_dates=0,
            dates_only_in_lean=len(lean_data),
            dates_only_in_cross=0,
            max_divergence_bp=Decimal("0"),
            rows_over_threshold=0,
            worst_rows=(),
            fetch_error=f"cross-source fetch failed: {type(exc).__name__}: {exc}",
        )

    return compute_diff(
        ticker=ticker,
        lean_data=lean_data,
        cross_data=cross_data,
        threshold_bp=threshold_bp,
        show_worst=show_worst,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help=(
            f"Root of the LEAN data directory containing "
            f"equity/usa/daily/<lower>.zip files (default: {DEFAULT_DATA_DIR})"
        ),
    )
    p.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help=f"Comma-separated ticker list (default: {','.join(DEFAULT_TICKERS)})",
    )
    p.add_argument(
        "--source",
        choices=("stooq", "tiingo"),
        default="stooq",
        help="Cross-source provider (default: stooq, no API key needed)",
    )
    p.add_argument(
        "--tiingo-api-key",
        default=None,
        help="Tiingo API key (only required with --source tiingo)",
    )
    p.add_argument(
        "--start",
        default=None,
        help=(
            "Inclusive cross-source start date YYYY-MM-DD "
            "(default: min(LEAN seeded dates) for each ticker)"
        ),
    )
    p.add_argument(
        "--end",
        default=None,
        help=(
            "Inclusive cross-source end date YYYY-MM-DD "
            "(default: max(LEAN seeded dates) for each ticker)"
        ),
    )
    p.add_argument(
        "--threshold-bp",
        type=Decimal,
        default=DEFAULT_THRESHOLD_BP,
        help=(
            f"Divergence threshold in basis points (default: {DEFAULT_THRESHOLD_BP}). "
            "A breach exists when |lean - cross| / cross * 10000 > threshold."
        ),
    )
    p.add_argument(
        "--show-worst",
        type=int,
        default=5,
        help=(
            "How many worst-divergence rows to print per ticker (default: 5). "
            "Set 0 to suppress the per-ticker detail."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (instead of the text table)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Write the JSON payload to this path (in addition to stdout)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"error: --data-dir {data_dir} is not a directory", file=sys.stderr)
        return 2

    tickers = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if not tickers:
        print("error: --tickers resolved to empty list", file=sys.stderr)
        return 2

    explicit_start: _date | None = (
        datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    )
    explicit_end: _date | None = (
        datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
    )

    if args.threshold_bp <= 0:
        print("error: --threshold-bp must be positive", file=sys.stderr)
        return 2

    if args.show_worst < 0:
        print("error: --show-worst must be non-negative", file=sys.stderr)
        return 2

    source: SourceName = args.source
    tiingo_key: str | None = args.tiingo_api_key
    if source == "tiingo" and not tiingo_key:
        # Fall through with empty key; verify_one will surface the error
        # per-ticker as fetch_error. We could fast-fail here but per-ticker
        # error reporting keeps the JSON payload uniform across modes.
        pass

    # Read each ticker's LEAN on-disk dates UP FRONT so the summary banner
    # can report the actual covered window. ``verify_one`` re-reads the zip
    # internally; that's redundant but cheap (single zip per ticker, ~6kB
    # each). Keeping the two reads independent keeps verify_one self-
    # contained for the unit-test path.
    per_ticker_iso_dates: dict[str, tuple[str, str] | None] = {}
    for ticker in tickers:
        zip_path = data_dir / "equity" / "usa" / "daily" / f"{ticker.lower()}.zip"
        try:
            lean_data = read_lean_zip(zip_path)
        except (FileNotFoundError, ValueError, zipfile.BadZipFile):
            per_ticker_iso_dates[ticker] = None
            continue
        if not lean_data:
            per_ticker_iso_dates[ticker] = None
            continue
        sorted_iso = sorted(lean_data.keys())
        per_ticker_iso_dates[ticker] = (sorted_iso[0], sorted_iso[-1])

    verifications: list[TickerVerification] = []
    for ticker in tickers:
        v = verify_one(
            ticker=ticker,
            data_dir=data_dir,
            source=source,
            tiingo_api_key=tiingo_key,
            explicit_start=explicit_start,
            explicit_end=explicit_end,
            threshold_bp=args.threshold_bp,
            show_worst=args.show_worst,
        )
        verifications.append(v)

    # Derive the reported window from the per-ticker on-disk dates we just
    # collected. Each ticker may have a slightly different range; pick the
    # widest span across all tickers (min of min, max of max) for the
    # human-readable banner.
    on_disk_starts = [
        datetime.strptime(rng[0], "%Y-%m-%d").date()
        for rng in per_ticker_iso_dates.values()
        if rng is not None
    ]
    on_disk_ends = [
        datetime.strptime(rng[1], "%Y-%m-%d").date()
        for rng in per_ticker_iso_dates.values()
        if rng is not None
    ]
    if explicit_start is not None:
        reported_start = explicit_start
    elif on_disk_starts:
        reported_start = min(on_disk_starts)
    else:
        # No tickers had readable LEAN data; fall back to today UTC. The
        # per-ticker JSON has the real story.
        reported_start = datetime.now(tz=UTC).date()
    if explicit_end is not None:
        reported_end = explicit_end
    elif on_disk_ends:
        reported_end = max(on_disk_ends)
    else:
        reported_end = datetime.now(tz=UTC).date()

    verifications_tuple = tuple(verifications)
    payload = _build_json_payload(
        source=source,
        data_dir=data_dir,
        start=reported_start,
        end=reported_end,
        threshold_bp=args.threshold_bp,
        verifications=verifications_tuple,
    )

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_text_summary(
            source=source,
            data_dir=data_dir,
            start=reported_start,
            end=reported_end,
            threshold_bp=args.threshold_bp,
            verifications=verifications_tuple,
        )

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(payload, indent=2) + "\n")
        if not args.json:
            print(f"JSON payload written to: {out_path}")

    any_fetch_error = any(v.fetch_error is not None for v in verifications_tuple)
    any_breach = any(
        v.rows_over_threshold > 0 for v in verifications_tuple if v.fetch_error is None
    )

    if any_fetch_error:
        return 2
    if any_breach:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
