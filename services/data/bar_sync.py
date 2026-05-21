"""services/data/bar_sync.py — daily bar sync from IBKR → LEAN on-disk format.

Option C of the 2026-05-20 data-layer pivot v2 (see
``Docs/decisions-log.md`` 2026-05-20 evening entry "Data-layer pivot
deploy ceremony: 3 sequential failure modes; lean_local stopped
pending v2 architecture"). The api owns the bar-sync responsibility:
fetches daily OHLCV bars for the Phase 1 universe from IBKR via a
dedicated ``ib-async`` connection on ``clientId=2`` (distinct from the
order-placement worker's ``clientId=1``), writes them to the shared
``lean_data`` Docker volume in LEAN's expected on-disk format
(equity-daily zip + futures-daily zip + per-day universe csv +
map_files sentinels), and ``lean_local`` reads via ``FakeDataQueue``
+ ``SubscriptionDataReaderHistoryProvider``.

Architectural notes
-------------------

* **Separate IBKR connection.** This module opens a SEPARATE ib-async
  connection to the existing ``ib_gateway`` sidecar — not a new
  gateway. IBKR allows multiple distinct clientIds per gateway
  session (verified 2026-05-19 evening probe). The connection
  lifecycle is short (~30-60s per cycle); the connect/disconnect
  overhead is negligible. Defense in depth: a bug or hang in
  bar_sync's read-only socket cannot backpressure the order worker's
  long-lived ``clientId=1`` socket.

* **clientId allocation** (per dev-guide §1.5 LOCKED + this PR):
    - ``clientId=1`` — api order-placement worker (services/execution/ibkr_adapter.py)
    - ``clientId=2`` — api bar_sync worker (this module)
    - ``clientId=80-99`` — operator probes + recovery tools
    - reserve ``3-7`` for future expansion

* **Futures strategy.** For each futures market the worker:
    1. Fetches continuous-mapped daily bars via ``reqHistoricalData``
       on an ``ib_async.ContFuture`` symbol — IBKR handles the roll
       math + returns a clean back-history.
    2. Resolves the *current* front-month expiry via
       ``reqContractDetails`` on the parent ``ib_async.Future``
       (keep contracts whose expiry has not yet passed; pick the
       earliest remaining).
    3. Writes ALL back-history bars under the CURRENT front-month
       expiry zip + a per-day universe file pointing at that current
       front-month for every day. The continuous-mapped bars are the
       authoritative "what was the active contract's close on day N"
       series; the per-day universe is synthesized but consistent
       with what LEAN's ``DataMappingMode.OPEN_INTEREST`` resolver
       picks today.

  This satisfies LEAN's on-disk-shape expectation (per-expiry trade
  zip + per-day universe CSV) under ``DataNormalizationMode.RAW``
  without requiring per-historical-expiry IBKR queries. For a
  daily-resolution trend-following strategy where the Donchian/MA/
  Hurst/ATR all derive from close prices, the continuous-mapped
  back-history is functionally equivalent to a per-expiry replay.

* **ETF strategy.** Simpler: fetch daily bars via ``reqHistoricalData``
  on a SMART-routed ``ib_async.Stock``, write the equity-daily zip
  with **deci-cent integer-scaled** prices (LEAN's equity-daily
  convention; $85.56 → ``855600``), plus a 2-row sentinel map_file
  + a 2-row sentinel factor_file (``price_factor=1``, ``split_factor=1``).

* **Scheduling.** Mirrors ``services.reconciliation.scheduler.ReconciliationScheduler``:
  stdlib asyncio + ``zoneinfo`` + injectable clock + ``should_fire_now``
  pure-policy helper + ``maybe_fire`` one-shot + ``run_forever``
  supervisor. Fires once per America/New_York calendar day at the
  configured ``sync_time_et`` (default ``17:00`` — 30 min before the
  LEAN strategy's 17:30 ET cycle so the fresh bars are on disk
  before LEAN reads).

A01 / A02 / A05 / A06 enforced.

* **A01** — no plaintext secrets in this module; IBKR creds come
  from the api config (sops-backed).
* **A02** — ``services/data/**`` is NOT on the forbidden whitelist;
  regular PR review applies.
* **A05** — Decimal-via-``str()`` at the ib-async boundary (the
  library returns float for OHLCV prices); writes preserve the
  Decimal precision through the formatting layer.
* **A06** — tz-aware UTC for ``now_utc`` inputs; America/New_York
  for ET-anchored session-date arithmetic via ``zoneinfo``.
"""

from __future__ import annotations

import asyncio
import math
import zipfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from zoneinfo import ZoneInfo

import structlog

if TYPE_CHECKING:  # pragma: no cover — type-only
    pass

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Constants — universe + scheduling locked here
# ---------------------------------------------------------------------------


_ET: Final[ZoneInfo] = ZoneInfo("America/New_York")

#: Default sync time in America/New_York. 17:00 ET is 30 min before LEAN's
#: 17:30 ET signal cycle — fresh bars land on disk before LEAN reads.
DEFAULT_SYNC_TIME_ET: Final[time] = time(hour=17, minute=0)

#: Default tick cadence — how often the scheduler checks whether the sync
#: time has been reached. 60s matches ReconciliationScheduler's pattern.
DEFAULT_TICK_INTERVAL_SECONDS: Final[float] = 60.0

#: Default count of daily bars fetched per market. ~250 calendar days =
#: ~250 trading days, comfortably above the V1 strategy warmup minimum
#: of 225 (MA_SLOW_DAYS=200 + ATR_LOOKBACK_DAYS=20 + 5-day pad).
DEFAULT_BARS_PER_FETCH: Final[int] = 250

#: Default LEAN data root inside the api container. Matches the
#: ``lean_data`` Docker volume mount at ``/Lean/Data`` (the same
#: volume LEAN reads from).
DEFAULT_DATA_ROOT: Final[Path] = Path("/Lean/Data")

#: Default per-IBKR-call deadline (seconds). reqHistoricalData on a
#: ContFuture with ~250 daily bars typically returns in <5s; we cap
#: at 60s so a hung call doesn't wedge the cycle.
DEFAULT_IBKR_CALL_TIMEOUT_SECONDS: Final[float] = 60.0

#: Default clientId for the bar_sync worker — distinct from the
#: order-placement worker's clientId=1 per dev-guide §1.5 LOCKED.
DEFAULT_BAR_SYNC_CLIENT_ID: Final[int] = 2

#: Default wall-clock budget for the front-month OI snapshot poll
#: (per futures market). IBKR's market-data ticks for futures
#: generic-tick 588 typically arrive within 1-2s of subscription;
#: 5s gives comfortable headroom without lengthening the cycle
#: materially. Set to 0 (or negative) on the BarSyncConfig to disable
#: the OI fetch entirely (universe files will then have empty OI
#: columns, equivalent to pre-2026-05-20 sub-pivot behavior).
DEFAULT_OI_WAIT_SECONDS: Final[float] = 5.0

#: IBKR generic-tick ID that triggers ``futuresOpenInterest`` population
#: on the returned :class:`ib_async.Ticker`. Verified against
#: ``ib_async/ib.py::reqMktData`` docstring + ``ib_async/wrapper.py``
#: tick-type map ``86: 'futuresOpenInterest'``.
_IBKR_FUTURES_OI_GENERIC_TICK: Final[str] = "588"


@dataclass(frozen=True, slots=True)
class MarketMeta:
    """Per-market on-disk + IBKR routing metadata.

    The 11 entries in :data:`PHASE1_UNIVERSE_METADATA` are the locked
    Phase 1 universe (4 ETFs + 7 micro futures) per
    ``strategies/v1_trend_following/parameters.py::V1_CANDIDATE_UNIVERSE``.

    Attributes:
        kind: ``"etf"`` for SMART-routed equities, ``"futures"`` for CME/
            COMEX/NYMEX micro futures.
        ibkr_symbol: the bare symbol IBKR uses (e.g., ``"TLT"`` or
            ``"MES"``). Futures use the un-prefixed form (no leading ``/``).
        ibkr_exchange: the IBKR primary-exchange string (e.g., ``"SMART"``,
            ``"CME"``, ``"COMEX"``, ``"NYMEX"``).
        market_dir: the LEAN on-disk market directory (lowercase). For
            ETFs always ``"usa"``; for futures one of ``"cme"`` /
            ``"comex"`` / ``"nymex"``.
        lean_market_code: the LEAN market code that lands in the
            map_file row (e.g., ``"P"`` for NYSE Arca, ``"CME"`` for
            CME-listed futures).
    """

    kind: Literal["etf", "futures"]
    ibkr_symbol: str
    ibkr_exchange: str
    market_dir: str
    lean_market_code: str


#: Locked Phase 1 universe metadata. Keys mirror the LEAN-side keys in
#: ``strategies.v1_trend_following.parameters.V1_CANDIDATE_UNIVERSE``
#: ("/MES" for futures, bare "TLT" for ETFs).
PHASE1_UNIVERSE_METADATA: Final[dict[str, MarketMeta]] = {
    # Micro futures — exchange codes match the LEAN futures-daily directory
    # convention (cme/comex/nymex).
    "/MES": MarketMeta("futures", "MES", "CME", "cme", "CME"),
    "/MNQ": MarketMeta("futures", "MNQ", "CME", "cme", "CME"),
    "/MYM": MarketMeta("futures", "MYM", "CBOT", "cme", "CME"),
    "/M2K": MarketMeta("futures", "M2K", "CME", "cme", "CME"),
    "/MGC": MarketMeta("futures", "MGC", "COMEX", "comex", "COMEX"),
    "/MCL": MarketMeta("futures", "MCL", "NYMEX", "nymex", "NYMEX"),
    "/MBT": MarketMeta("futures", "MBT", "CME", "cme", "CME"),
    # ETFs — SMART-routed; map_files use the primary listing exchange code
    # (P = NYSE Arca, the listing venue for all 4 Phase 1 bond ETFs).
    "TLT": MarketMeta("etf", "TLT", "SMART", "usa", "P"),
    "IEF": MarketMeta("etf", "IEF", "SMART", "usa", "P"),
    "SHY": MarketMeta("etf", "SHY", "SMART", "usa", "P"),
    "TIP": MarketMeta("etf", "TIP", "SMART", "usa", "P"),
}


# ---------------------------------------------------------------------------
# Pure-policy data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """A single daily OHLCV bar.

    Prices stored as ``Decimal`` to preserve precision through the
    ib-async ``float`` boundary (coerced via ``Decimal(str(x))`` per
    [A05]). ``session_date`` is anchored on the ET wall clock (not UTC)
    — IBKR's daily bars are session-relative.
    """

    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class MarketSyncResult:
    """Outcome of syncing a single market in one cycle.

    Either ``success=True`` with populated bar counts, or
    ``success=False`` with the ``error`` string set. Mutually
    exclusive — both branches always populate the metadata fields
    (last_session_date may be None on the first-ever sync of a market
    that had no historical data).

    ``open_interest`` is the snapshot value fetched from IBKR for the
    futures front-month expiry (``None`` for ETFs and for failed
    syncs; ``0`` when the OI fetch ran but produced no usable value;
    a positive int when LEAN's resolver will have a real value to
    pick on).
    """

    market: str
    success: bool
    bars_written: int
    last_session_date: date | None
    front_month_expiry: str | None  # YYYYMM for futures; None for ETFs / failed syncs
    error: str | None
    open_interest: int | None = (
        None  # None for ETFs; 0 on fetch failure; positive int when populated
    )


@dataclass(frozen=True, slots=True)
class BarSyncCycleResult:
    """Outcome of one bar-sync cycle (all markets attempted).

    Always includes both successful + failed; len(successful) +
    len(failed) == len(markets attempted). The cycle is considered
    a partial success if any market succeeded.
    """

    cycle_started_at_utc: datetime
    cycle_completed_at_utc: datetime
    successful_markets: tuple[MarketSyncResult, ...]
    failed_markets: tuple[MarketSyncResult, ...]

    @property
    def all_failed(self) -> bool:
        return len(self.successful_markets) == 0 and len(self.failed_markets) > 0

    @property
    def total_markets(self) -> int:
        return len(self.successful_markets) + len(self.failed_markets)


@dataclass(frozen=True, slots=True)
class BarSyncConfig:
    """Configuration knobs for the bar_sync worker.

    Most fields default to sane production values; tests override via
    the dataclass constructor.

    Attributes:
        markets: Mapping market-key → MarketMeta. Defaults to the
            locked Phase 1 universe; tests can pass a 1-entry dict to
            exercise a single market without standing up the full 11.
        data_root: LEAN on-disk root (``/Lean/Data`` in production).
        bars_per_fetch: Count of daily bars per ``reqHistoricalData`` call.
        sync_time_et: Wall-clock time of day (ET) at which the cycle
            fires.
        tick_interval_seconds: How often the scheduler checks whether
            sync_time_et has been reached.
        ibkr_host / ibkr_port / ibkr_client_id: ib-async connection
            parameters. Default clientId=2 per dev-guide §1.5 LOCKED.
        ibkr_account: optional account number (most accounts have a
            single default and don't need this).
        ibkr_call_timeout_seconds: per-call wall-clock deadline on
            ib-async awaits.
        ibkr_connect_timeout_seconds: max wall-clock for the initial
            connect handshake.
    """

    markets: dict[str, MarketMeta] = field(
        default_factory=lambda: dict(PHASE1_UNIVERSE_METADATA),
    )
    data_root: Path = DEFAULT_DATA_ROOT
    bars_per_fetch: int = DEFAULT_BARS_PER_FETCH
    sync_time_et: time = DEFAULT_SYNC_TIME_ET
    tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS
    ibkr_host: str = "ib_gateway"
    ibkr_port: int = 4004
    ibkr_client_id: int = DEFAULT_BAR_SYNC_CLIENT_ID
    ibkr_account: str | None = None
    ibkr_call_timeout_seconds: float = DEFAULT_IBKR_CALL_TIMEOUT_SECONDS
    ibkr_connect_timeout_seconds: float = 30.0
    #: Wall-clock budget for the front-month OI snapshot poll (futures only).
    #: Set to 0 to skip OI fetch entirely; the bundle writer will still
    #: produce a (single-expiry, empty-OI) universe file in that case.
    oi_wait_seconds: float = DEFAULT_OI_WAIT_SECONDS


# ---------------------------------------------------------------------------
# Pure-policy schedule helpers
# ---------------------------------------------------------------------------


def should_fire_now(
    *,
    now_utc: datetime,
    sync_time_et: time,
    last_fired_session_date_et: date | None,
) -> bool:
    """Pure-policy: should the scheduler fire at this wall-clock moment?

    Fires when:
      1. ``now_utc`` converted to ET is at or past ``sync_time_et``
         for the current ET calendar date, AND
      2. ``last_fired_session_date_et`` is not the current ET calendar
         date (i.e., we haven't already fired today).

    Mirrors :func:`services.reconciliation.scheduler.should_fire_now`.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (A06)")
    et_now = now_utc.astimezone(_ET)
    if et_now.time() < sync_time_et:
        return False
    today_et = et_now.date()
    if last_fired_session_date_et == today_et:
        return False
    return True


def current_session_date_et(now_utc: datetime) -> date:
    """Convert a UTC datetime to the America/New_York calendar date."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (A06)")
    return now_utc.astimezone(_ET).date()


# ---------------------------------------------------------------------------
# Pure-policy format helpers + LEAN-shape path computations
# ---------------------------------------------------------------------------


def _format_futures_price(value: Decimal) -> str:
    """Format a price as the LEAN futures-daily on-disk style.

    Matches the LEAN tutorial bundle's es_trade.csv: integer prices
    print bare (``"5810"``), fractional prices print with up to 4
    decimal places and trailing zeros stripped (``"7378.25"``,
    ``"104.15"``).

    The seed_lean_futures_databento.py canonical implementation
    (preserved in git history pre-deletion at commit ``9c39f6e^``)
    used the same convention; this re-derivation matches byte-for-byte.
    """
    int_value = int(value)
    if value == Decimal(int_value):
        return str(int_value)
    # Quantize via Decimal not float to avoid float-binary precision drift.
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _equity_daily_csv_line(bar: Bar) -> str:
    """One CSV row in the LEAN equity-daily format (deci-cent scaled)."""
    o_scaled = int(bar.open * 10000)
    h_scaled = int(bar.high * 10000)
    l_scaled = int(bar.low * 10000)
    c_scaled = int(bar.close * 10000)
    return (
        f"{bar.session_date:%Y%m%d} 00:00,{o_scaled},{h_scaled},{l_scaled},{c_scaled},{bar.volume}"
    )


def build_equity_daily_csv(bars: Iterable[Bar]) -> bytes:
    """LEAN equity-daily CSV body (UTF-8 encoded; trailing newline).

    Format per `scripts/seed_lean_data.py` (deleted 2026-05-20, recovered
    from git history at commit ``9c39f6e^``):

      ``YYYYMMDD 00:00,O*10000,H*10000,L*10000,C*10000,V``

    Prices are integer-scaled by 10000 (deci-cents). A close of
    $85.56 round-trips as ``855600``. Volume is raw integer shares.
    Bars must be in ascending session-date order; the caller is
    responsible for sorting + dedup.
    """
    lines = [_equity_daily_csv_line(b) for b in bars]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_equity_map_file(ticker: str, exchange: str) -> str:
    """LEAN map_files 2-row sentinel for an equity.

    Format: ``YYYYMMDD,<lower>,<exchange>``. First row is the inception
    sentinel (``19980102`` is LEAN's universal pre-history anchor);
    second row is the far-future sentinel (``20501231``).
    """
    lower = ticker.lower()
    return f"19980102,{lower},{exchange}\n20501231,{lower},{exchange}\n"


def build_equity_factor_file(ref_price: Decimal) -> str:
    """LEAN factor_files 2-row sentinel.

    Format: ``YYYYMMDD,<price_factor>,<split_factor>,<ref_price>``.
    The strategy operates on un-adjusted prices (Donchian/MA/Hurst/ATR
    over raw closes) so we hard-code price_factor=1 + split_factor=1.
    The second row's ref_price=0 is LEAN's far-future sentinel
    convention.
    """
    return f"19980102,1,1,{ref_price:.4f}\n20501231,1,1,0\n"


def _futures_trade_csv_line(bar: Bar) -> str:
    """One CSV row in the LEAN futures-daily trade format (raw float prices)."""
    return (
        f"{bar.session_date:%Y%m%d} 00:00,"
        f"{_format_futures_price(bar.open)},"
        f"{_format_futures_price(bar.high)},"
        f"{_format_futures_price(bar.low)},"
        f"{_format_futures_price(bar.close)},"
        f"{bar.volume}"
    )


def build_futures_trade_csv(bars: Iterable[Bar]) -> bytes:
    """LEAN futures-daily trade CSV body (UTF-8 encoded; trailing newline).

    Format per `scripts/seed_lean_futures_databento.py` (deleted 2026-05-20,
    recovered from git history):

      ``YYYYMMDD 00:00,O,H,L,C,V`` — raw float prices (NOT deci-cent
      scaled). Volume is raw integer contracts.
    """
    lines = [_futures_trade_csv_line(b) for b in bars]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _futures_oi_csv_line(session_date: date, oi: int) -> str:
    return f"{session_date:%Y%m%d} 00:00,{oi}"


def build_futures_oi_csv(bars: Iterable[Bar], oi: int) -> bytes:
    """LEAN futures-daily open-interest CSV body.

    Format per the LEAN tutorial bundle: ``YYYYMMDD 00:00,<oi>``.

    The bar_sync worker doesn't fetch per-bar IBKR OI (the
    ``reqHistoricalData`` ``whatToShow=TRADES`` path only returns
    OHLCV); we synthesize a single constant OI value (typically
    derived from ``reqContractDetails`` or set to 0 if unavailable)
    across the back-history. LEAN uses OI only for the
    ``DataMappingMode.OPEN_INTEREST`` resolver to pick the active
    contract; since the per-day universe file pins the active contract
    explicitly, the per-bar OI is informational rather than
    load-bearing.
    """
    lines = [_futures_oi_csv_line(b.session_date, oi) for b in bars]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _futures_universe_row(expiry_yyyymm: str, bar: Bar, oi: int | None) -> str:
    """One row of the per-day universe CSV.

    Format per the LEAN tutorial bundle:

      ``<expiry>,<open>,<high>,<low>,<close>,<volume>,<open_interest>``

    ``open_interest`` may be empty when unknown.
    """
    oi_str = "" if oi is None else str(oi)
    return (
        f"{expiry_yyyymm},"
        f"{_format_futures_price(bar.open)},"
        f"{_format_futures_price(bar.high)},"
        f"{_format_futures_price(bar.low)},"
        f"{_format_futures_price(bar.close)},"
        f"{bar.volume},"
        f"{oi_str}"
    )


def build_futures_universe_csv(expiry_yyyymm: str, bar: Bar, oi: int | None) -> bytes:
    """One per-day universe file body (single-contract-active variant).

    The bar_sync worker writes per-day universe files pinning the
    current front-month as the sole active contract for every day in
    the back-history. This is the simplifying insight that lets us
    avoid per-historical-expiry IBKR queries — LEAN's resolver picks
    "the only contract in today's universe file" without consulting
    real historical front-month rotations.

    Header line + one data row per file:

      ``#expiry,open,high,low,close,volume,open_interest\\n<expiry>,...``
    """
    header = "#expiry,open,high,low,close,volume,open_interest"
    return (header + "\n" + _futures_universe_row(expiry_yyyymm, bar, oi) + "\n").encode("utf-8")


def build_futures_map_file(ticker: str, market_code: str) -> str:
    """LEAN map_files 2-row sentinel for a futures market.

    Format (Path 4 / Raw-mode insight, per the deleted DataBento
    seeder's canonical output):

      ``18991230,<lower>\\n20501231,<lower>,<MARKET_CODE>``

    Note the first row has no market_code; the second row has it.
    This is the form that works under ``DataNormalizationMode.RAW``
    where the strategy doesn't depend on QC-internal continuous-
    contract scaling math.
    """
    lower = ticker.lower()
    return f"18991230,{lower}\n20501231,{lower},{market_code}\n"


# ---------------------------------------------------------------------------
# Path computations (LEAN on-disk layout)
# ---------------------------------------------------------------------------


def equity_daily_zip_path(data_root: Path, ticker: str) -> Path:
    """``<data_root>/equity/usa/daily/<lower>.zip``"""
    return data_root / "equity" / "usa" / "daily" / f"{ticker.lower()}.zip"


def equity_map_file_path(data_root: Path, ticker: str) -> Path:
    """``<data_root>/equity/usa/map_files/<lower>.csv``"""
    return data_root / "equity" / "usa" / "map_files" / f"{ticker.lower()}.csv"


def equity_factor_file_path(data_root: Path, ticker: str) -> Path:
    """``<data_root>/equity/usa/factor_files/<lower>.csv``"""
    return data_root / "equity" / "usa" / "factor_files" / f"{ticker.lower()}.csv"


def futures_trade_zip_path(data_root: Path, ticker: str, market_dir: str) -> Path:
    """``<data_root>/future/<market_dir>/daily/<lower>_trade.zip``"""
    return data_root / "future" / market_dir / "daily" / f"{ticker.lower()}_trade.zip"


def futures_oi_zip_path(data_root: Path, ticker: str, market_dir: str) -> Path:
    """``<data_root>/future/<market_dir>/daily/<lower>_openinterest.zip``"""
    return data_root / "future" / market_dir / "daily" / f"{ticker.lower()}_openinterest.zip"


def futures_universe_file_path(
    data_root: Path,
    ticker: str,
    market_dir: str,
    session_date: date,
) -> Path:
    """``<data_root>/future/<market_dir>/universes/<lower>/<YYYYMMDD>.csv``"""
    return (
        data_root
        / "future"
        / market_dir
        / "universes"
        / ticker.lower()
        / f"{session_date:%Y%m%d}.csv"
    )


def futures_map_file_path(data_root: Path, ticker: str, market_dir: str) -> Path:
    """``<data_root>/future/<market_dir>/map_files/<lower>.csv``"""
    return data_root / "future" / market_dir / "map_files" / f"{ticker.lower()}.csv"


# ---------------------------------------------------------------------------
# Filesystem writers (sync; tested without IBKR)
# ---------------------------------------------------------------------------


def write_zip_with_member(zip_path: Path, member_name: str, body: bytes) -> int:
    """Write ``body`` into ``zip_path`` as a single member; return file size.

    Atomic-ish: writes via ``ZipFile.writestr`` then closes. If the
    parent directory is missing, creates it. Existing zip is overwritten.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member_name, body)
    return zip_path.stat().st_size


def write_etf_bundle(
    *,
    data_root: Path,
    ticker: str,
    exchange: str,
    bars: list[Bar],
) -> int:
    """Write the full equity-daily bundle for one ticker.

    Emits 3 files:
      * ``equity/usa/daily/<lower>.zip`` containing ``<lower>.csv``
      * ``equity/usa/map_files/<lower>.csv`` (2-row sentinel)
      * ``equity/usa/factor_files/<lower>.csv`` (2-row sentinel)

    Returns the zip's size in bytes. Raises ``ValueError`` if ``bars``
    is empty (no data → can't write a valid bundle + no ref_price for
    the factor_file).
    """
    if not bars:
        raise ValueError(f"write_etf_bundle: bars is empty for {ticker!r}")
    csv_bytes = build_equity_daily_csv(bars)
    zip_path = equity_daily_zip_path(data_root, ticker)
    zip_size = write_zip_with_member(zip_path, f"{ticker.lower()}.csv", csv_bytes)
    map_path = equity_map_file_path(data_root, ticker)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(build_equity_map_file(ticker, exchange))
    factor_path = equity_factor_file_path(data_root, ticker)
    factor_path.parent.mkdir(parents=True, exist_ok=True)
    # Use the most recent close as the factor_file's reference price.
    factor_path.write_text(build_equity_factor_file(bars[-1].close))
    return zip_size


def write_futures_bundle(
    *,
    data_root: Path,
    ticker: str,
    market_dir: str,
    market_code: str,
    front_month_expiry_yyyymm: str,
    bars: list[Bar],
    open_interest: int = 0,
) -> int:
    """Write the full futures-daily bundle for one ticker.

    Emits 4 paths:
      * ``future/<market_dir>/daily/<lower>_trade.zip`` containing
        ``<lower>_trade_<YYYYMM>.csv`` (the front-month bucket)
      * ``future/<market_dir>/daily/<lower>_openinterest.zip``
        containing ``<lower>_openinterest_<YYYYMM>.csv``
      * ``future/<market_dir>/universes/<lower>/<YYYYMMDD>.csv`` per
        session-date in ``bars`` (one file per bar)
      * ``future/<market_dir>/map_files/<lower>.csv`` (2-row sentinel)

    Returns the trade-zip's size in bytes. Raises ``ValueError`` if
    ``bars`` is empty.
    """
    if not bars:
        raise ValueError(f"write_futures_bundle: bars is empty for {ticker!r}")
    trade_csv_bytes = build_futures_trade_csv(bars)
    trade_zip = futures_trade_zip_path(data_root, ticker, market_dir)
    trade_member = f"{ticker.lower()}_trade_{front_month_expiry_yyyymm}.csv"
    trade_size = write_zip_with_member(trade_zip, trade_member, trade_csv_bytes)
    oi_csv_bytes = build_futures_oi_csv(bars, open_interest)
    oi_zip = futures_oi_zip_path(data_root, ticker, market_dir)
    oi_member = f"{ticker.lower()}_openinterest_{front_month_expiry_yyyymm}.csv"
    write_zip_with_member(oi_zip, oi_member, oi_csv_bytes)
    # Per-day universe files — one per session_date in bars.
    universe_oi = open_interest if open_interest > 0 else None
    for bar in bars:
        u_path = futures_universe_file_path(data_root, ticker, market_dir, bar.session_date)
        u_path.parent.mkdir(parents=True, exist_ok=True)
        u_body = build_futures_universe_csv(front_month_expiry_yyyymm, bar, universe_oi)
        u_path.write_bytes(u_body)
    # 2-row sentinel map_file.
    map_path = futures_map_file_path(data_root, ticker, market_dir)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(build_futures_map_file(ticker, market_code))
    return trade_size


# ---------------------------------------------------------------------------
# IBKR fetcher — ib-async I/O
# ---------------------------------------------------------------------------


def _coerce_decimal(value: Any) -> Decimal:
    """Coerce ib-async ``float`` → ``Decimal`` per [A05].

    Pre-condition: ``value`` must be finite (not NaN, not inf). The
    caller (``_bar_data_to_bar``) checks via ``math.isnan(...)`` and
    skips invalid rows; this helper raises ``ValueError`` defensively
    if a NaN slips through.
    """
    try:
        if math.isnan(float(value)) or math.isinf(float(value)):
            raise ValueError(f"_coerce_decimal: non-finite value {value!r}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"_coerce_decimal: cannot coerce {value!r}: {exc}") from exc
    return Decimal(str(value))


def _bar_data_to_bar(bar_data: Any) -> Bar | None:
    """Translate an ib-async ``BarData`` → :class:`Bar`.

    Returns None if any OHLCV field is missing or non-finite. ib-async
    occasionally emits placeholder bars (``close=-1`` is the canonical
    "no data" sentinel for the historical-data path); we drop them
    rather than letting them poison the on-disk file.
    """
    raw_date = getattr(bar_data, "date", None)
    raw_open = getattr(bar_data, "open", None)
    raw_high = getattr(bar_data, "high", None)
    raw_low = getattr(bar_data, "low", None)
    raw_close = getattr(bar_data, "close", None)
    raw_volume = getattr(bar_data, "volume", 0)
    if (
        raw_date is None
        or raw_open is None
        or raw_high is None
        or raw_low is None
        or raw_close is None
    ):
        return None
    # ib-async returns date as datetime.date OR datetime.datetime depending
    # on whether the bar is daily (date) or intraday (datetime). For
    # daily-resolution bars, .date() is identity on a date; for datetime,
    # extract the calendar-date portion.
    if isinstance(raw_date, datetime):
        session_date = raw_date.date()
    elif isinstance(raw_date, date):
        session_date = raw_date
    else:
        # Some IBKR feeds return YYYYMMDD strings; defensive parse.
        try:
            session_date = datetime.strptime(str(raw_date), "%Y%m%d").date()
        except ValueError:
            return None
    try:
        o = _coerce_decimal(raw_open)
        h = _coerce_decimal(raw_high)
        low = _coerce_decimal(raw_low)
        c = _coerce_decimal(raw_close)
    except ValueError:
        return None
    if c <= 0:
        # IBKR emits close=-1 for "no data this session" — drop.
        return None
    # Volume can be -1 from IBKR for "unavailable"; coerce to 0.
    try:
        vol = int(raw_volume)
    except (TypeError, ValueError):
        vol = 0
    if vol < 0:
        vol = 0
    return Bar(session_date=session_date, open=o, high=h, low=low, close=c, volume=vol)


def parse_ibkr_bars(bar_datas: Iterable[Any]) -> list[Bar]:
    """Parse an iterable of ib-async ``BarData`` → ``list[Bar]``.

    Sorts ascending by session_date + de-duplicates (keeps the LAST
    bar per session_date, mirroring IBKR's "latest update wins"
    semantic). Returns an empty list if every input was invalid.
    """
    seen: dict[date, Bar] = {}
    for bar_data in bar_datas:
        b = _bar_data_to_bar(bar_data)
        if b is None:
            continue
        seen[b.session_date] = b
    return sorted(seen.values(), key=lambda b: b.session_date)


def pick_front_month_expiry(
    contract_details: Iterable[Any],
    *,
    today: date,
) -> str | None:
    """Pick the current front-month YYYYMM from a list of ib-async ``ContractDetails``.

    For each ContractDetails, the underlying ``contract.lastTradeDateOrContractMonth``
    field carries the expiry as a string like ``"20260620"`` (full date)
    or ``"202606"`` (year+month). Both shapes are accepted; we treat
    the year+month form as if the expiry were the last day of that month
    (anything not yet ended on ``today`` is still a candidate).

    Returns the smallest (earliest) expiry whose effective last-trade
    date is on or after ``today``. Returns None if no remaining
    contracts are found (caller should treat as a fetch error).
    """
    candidates: list[tuple[date, str]] = []
    for cd in contract_details:
        contract = getattr(cd, "contract", None)
        if contract is None:
            continue
        raw = getattr(contract, "lastTradeDateOrContractMonth", None)
        if not raw:
            continue
        raw_str = str(raw)
        expiry_yyyymm: str | None = None
        candidate_date: date | None = None
        if len(raw_str) >= 8:
            try:
                candidate_date = datetime.strptime(raw_str[:8], "%Y%m%d").date()
                expiry_yyyymm = raw_str[:6]
            except ValueError:
                candidate_date = None
        if candidate_date is None and len(raw_str) >= 6:
            try:
                year = int(raw_str[:4])
                month = int(raw_str[4:6])
                # Last day of month — pick 28th as a conservative inside-bound
                # since we only need monotonic ordering; the exact day is
                # immaterial for "is this expiry past?" decisions on daily
                # resolution.
                candidate_date = _date(year, month, 28)
                expiry_yyyymm = f"{year:04d}{month:02d}"
            except ValueError:
                continue
        if candidate_date is None or expiry_yyyymm is None:
            continue
        if candidate_date < today:
            continue
        candidates.append((candidate_date, expiry_yyyymm))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


HistoricalDataFetcher = Callable[[Any, str, int, float], Awaitable[list[Any]]]
"""Type alias for the fetch_etf_bars / fetch_futures_bars I/O seam.

Tests inject a stub that returns a fixed ``list[Any]`` of bar-shaped
objects; production uses the real ib-async path.
"""


async def fetch_etf_bars(
    ib: Any,
    market_key: str,
    *,
    meta: MarketMeta,
    bars_count: int,
    call_timeout_seconds: float,
) -> list[Bar]:
    """Fetch ``bars_count`` daily bars for an ETF via ib-async.

    Uses ``ib_async.Stock(symbol, 'SMART', 'USD')`` for the contract +
    ``reqHistoricalDataAsync(endDateTime='', durationStr='<N> D',
    barSizeSetting='1 day', whatToShow='TRADES', useRTH=True,
    formatDate=2)`` — formatDate=2 returns UTC datetimes (avoids local-
    time ambiguity).

    Wrapped in ``asyncio.wait_for(..., call_timeout_seconds)`` so a
    hung IBKR call doesn't wedge the cycle.
    """
    if meta.kind != "etf":
        raise ValueError(f"fetch_etf_bars called with non-ETF meta {meta!r}")
    from ib_async import Stock  # local import — same lazy-load pattern as ibkr_adapter

    contract = Stock(meta.ibkr_symbol, meta.ibkr_exchange, "USD")
    duration = f"{bars_count} D"
    bar_datas = await asyncio.wait_for(
        ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        ),
        timeout=call_timeout_seconds,
    )
    return parse_ibkr_bars(bar_datas or [])


async def fetch_futures_bars_and_front_month(
    ib: Any,
    market_key: str,
    *,
    meta: MarketMeta,
    bars_count: int,
    call_timeout_seconds: float,
    today: date,
) -> tuple[list[Bar], str]:
    """Fetch continuous-mapped daily bars + resolve front-month expiry.

    Two ib-async calls:
      1. ``reqHistoricalDataAsync(ContFuture(...))`` — daily bars,
         continuous-mapped by IBKR (front-month rolled forward as
         expiries pass).
      2. ``reqContractDetails(Future(symbol, '', exchange))`` —
         enumerate the chain → :func:`pick_front_month_expiry`.

    Returns ``(bars, front_month_yyyymm)``. Raises ``ValueError`` if
    no live contracts remain (clean operator signal that something is
    wrong with the symbology).
    """
    if meta.kind != "futures":
        raise ValueError(
            f"fetch_futures_bars_and_front_month called with non-futures meta {meta!r}"
        )
    from ib_async import ContFuture, Future

    cont_contract = ContFuture(meta.ibkr_symbol, meta.ibkr_exchange)
    duration = f"{bars_count} D"
    bar_datas = await asyncio.wait_for(
        ib.reqHistoricalDataAsync(
            cont_contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=False,  # futures trade ~23h; useRTH=False captures the full session
            formatDate=2,
        ),
        timeout=call_timeout_seconds,
    )
    bars = parse_ibkr_bars(bar_datas or [])

    chain_contract = Future(symbol=meta.ibkr_symbol, exchange=meta.ibkr_exchange)
    details = await asyncio.wait_for(
        ib.reqContractDetailsAsync(chain_contract),
        timeout=call_timeout_seconds,
    )
    front_month = pick_front_month_expiry(details or [], today=today)
    if front_month is None:
        raise ValueError(
            f"fetch_futures_bars_and_front_month: no live front-month for {market_key!r} "
            f"(reqContractDetails returned {len(details or [])} rows; symbology issue?)"
        )
    return bars, front_month


async def fetch_front_month_open_interest(
    ib: Any,
    market_key: str,
    *,
    meta: MarketMeta,
    front_month_expiry_yyyymm: str,
    wait_seconds: float = DEFAULT_OI_WAIT_SECONDS,
    poll_interval_seconds: float = 0.1,
) -> int:
    """Fetch the current open-interest snapshot for the front-month future.

    Implementation: ``ib.reqMktData(contract, genericTickList="588",
    snapshot=False)`` subscribes to streaming ticks (snapshot=True does
    NOT include generic ticks per IBKR's API contract). The returned
    :class:`ib_async.Ticker` populates asynchronously as ticks arrive;
    we poll ``ticker.futuresOpenInterest`` until it becomes non-NaN
    (typically 1-2s) or the wall-clock budget elapses. Subscription is
    cancelled in ``finally`` regardless of outcome.

    Returns the snapshot value as ``int`` (IBKR's tick is float but OI
    is conceptually a count). Returns ``0`` on any failure mode:

    * ``wait_seconds`` elapsed without the tick arriving
    * The ticker's ``futuresOpenInterest`` stays NaN (no subscription
      entitlement or the contract has no published OI yet)
    * The contract construction or ``reqMktData`` call raises

    Returning 0 keeps the caller's contract simple — the cycle still
    writes a usable LEAN bundle, just with empty-OI universe rows
    (which is the pre-fix behavior). Real OI > 0 is what unblocks
    LEAN's ``DataMappingMode.OPEN_INTEREST`` resolver downstream.

    Notes
    -----

    * The front-month ``Future`` contract is built explicitly with
      ``lastTradeDateOrContractMonth`` set so IBKR returns the OI of
      that specific expiry — distinct from the ContFuture used for
      historical-bar fetches (ContFuture has no OI; it's a synthetic
      continuous series).
    * IBKR's futures-OI tick is published once per contract per
      streaming subscription — it does NOT replay on resubscribe
      within the same TWS session. Cycling clientId=2 connect/
      disconnect per cycle (as bar_sync does) guarantees a fresh
      subscription each fire.
    """
    if meta.kind != "futures":
        raise ValueError(f"fetch_front_month_open_interest called with non-futures meta {meta!r}")
    if wait_seconds <= 0:
        return 0

    bound_log = log.bind(
        op="fetch_front_month_open_interest",
        market=market_key,
        front_month_expiry=front_month_expiry_yyyymm,
    )

    try:
        from ib_async import Future
    except ImportError:  # pragma: no cover — defensive; production has ib_async
        bound_log.exception("oi_fetch_ib_async_import_failed")
        return 0

    contract: Any
    try:
        contract = Future(
            symbol=meta.ibkr_symbol,
            exchange=meta.ibkr_exchange,
            lastTradeDateOrContractMonth=front_month_expiry_yyyymm,
        )
    except Exception:  # pragma: no cover — Future ctor is dataclass-like
        bound_log.exception("oi_fetch_contract_build_failed")
        return 0

    # ib-async requires a qualified contract before ``reqMktData`` because
    # the wrapper's ``startTicker`` calls ``hash(contract)`` which raises
    # ``ValueError: ... can't be hashed because no 'conId' value exists``
    # for unqualified Future objects. ``qualifyContractsAsync`` populates
    # ``conId`` (and ``localSymbol``/``tradingClass``/etc.) in-place via a
    # ``reqContractDetails`` roundtrip — typically ~1s per contract.
    # See ib_async/contract.py:174 (hash guard) + ib_async/wrapper.py:406
    # (startTicker → hash call site) + ib_async/ib.py:2110
    # (qualifyContractsAsync impl).
    try:
        qualify = getattr(ib, "qualifyContractsAsync", None)
        if qualify is None:
            # Older ib-async or a non-conforming fake — degrade gracefully.
            bound_log.warning("oi_fetch_qualify_unavailable")
            return 0
        qualified = await asyncio.wait_for(qualify(contract), timeout=max(wait_seconds, 1.0))
    except Exception:
        bound_log.exception("oi_fetch_qualify_failed")
        return 0
    # qualifyContractsAsync mutates the input contract in-place AND returns
    # a list of qualified contracts (one per input). A failure to qualify
    # (unknown symbol, ambiguous contract w/o filters) yields ``None`` in
    # the result slot. Accept either ordering — defensive against minor
    # ib-async behavioral changes.
    qualified_list = list(qualified or [])
    if not qualified_list or qualified_list[0] is None:
        bound_log.warning("oi_fetch_qualify_returned_no_match")
        return 0
    # Prefer the returned qualified contract over the in-place mutated one
    # (functionally identical but safer if a future ib-async stops mutating).
    contract = qualified_list[0]

    ticker: Any
    try:
        ticker = ib.reqMktData(
            contract,
            genericTickList=_IBKR_FUTURES_OI_GENERIC_TICK,
            snapshot=False,
        )
    except Exception:
        bound_log.exception("oi_fetch_reqmktdata_failed")
        return 0

    try:
        deadline = asyncio.get_event_loop().time() + wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            raw_oi = getattr(ticker, "futuresOpenInterest", float("nan"))
            if raw_oi is None:
                await asyncio.sleep(poll_interval_seconds)
                continue
            try:
                oi_float = float(raw_oi)
            except (TypeError, ValueError):
                await asyncio.sleep(poll_interval_seconds)
                continue
            if math.isnan(oi_float) or math.isinf(oi_float):
                await asyncio.sleep(poll_interval_seconds)
                continue
            oi_int = int(oi_float)
            if oi_int <= 0:
                # Some IBKR feeds emit OI=0 sentinel before real data arrives;
                # keep polling within the budget rather than returning prematurely.
                await asyncio.sleep(poll_interval_seconds)
                continue
            bound_log.info("oi_fetch_completed", open_interest=oi_int)
            return oi_int
        bound_log.warning(
            "oi_fetch_timeout",
            wait_seconds=wait_seconds,
        )
        return 0
    finally:
        cancel = getattr(ib, "cancelMktData", None)
        if cancel is not None:
            try:
                cancel(contract)
            except Exception:
                bound_log.exception("oi_fetch_cancel_failed")


# ---------------------------------------------------------------------------
# Per-market sync orchestrator
# ---------------------------------------------------------------------------


async def sync_one_market(
    ib: Any,
    market_key: str,
    meta: MarketMeta,
    *,
    config: BarSyncConfig,
    today: date,
) -> MarketSyncResult:
    """Fetch + write one market. Returns a ``MarketSyncResult``.

    Catches all exceptions (including ``asyncio.TimeoutError`` from
    the per-call wrappers) and packages them into the result's
    ``error`` field. Never raises — the worker calls this per market
    and aggregates results into a cycle report.
    """
    try:
        if meta.kind == "etf":
            bars = await fetch_etf_bars(
                ib,
                market_key,
                meta=meta,
                bars_count=config.bars_per_fetch,
                call_timeout_seconds=config.ibkr_call_timeout_seconds,
            )
            if not bars:
                return MarketSyncResult(
                    market=market_key,
                    success=False,
                    bars_written=0,
                    last_session_date=None,
                    front_month_expiry=None,
                    error="ibkr_returned_no_bars",
                )
            write_etf_bundle(
                data_root=config.data_root,
                ticker=meta.ibkr_symbol,
                exchange=meta.lean_market_code,
                bars=bars,
            )
            return MarketSyncResult(
                market=market_key,
                success=True,
                bars_written=len(bars),
                last_session_date=bars[-1].session_date,
                front_month_expiry=None,
                error=None,
            )
        else:
            bars, front_month = await fetch_futures_bars_and_front_month(
                ib,
                market_key,
                meta=meta,
                bars_count=config.bars_per_fetch,
                call_timeout_seconds=config.ibkr_call_timeout_seconds,
                today=today,
            )
            if not bars:
                return MarketSyncResult(
                    market=market_key,
                    success=False,
                    bars_written=0,
                    last_session_date=None,
                    front_month_expiry=front_month,
                    error="ibkr_returned_no_bars",
                )
            # Fetch the front-month OI snapshot AFTER bars + expiry are
            # resolved. Failure to obtain OI does NOT fail the cycle —
            # the helper returns 0 on any error, which the bundle writer
            # treats as "OI unknown" (empty universe-file column +
            # zero-line OI zip; equivalent to pre-fix behavior). Real
            # OI > 0 is what unblocks LEAN's DataMappingMode.OPEN_INTEREST
            # resolver downstream.
            open_interest = await fetch_front_month_open_interest(
                ib,
                market_key,
                meta=meta,
                front_month_expiry_yyyymm=front_month,
                wait_seconds=config.oi_wait_seconds,
            )
            write_futures_bundle(
                data_root=config.data_root,
                ticker=meta.ibkr_symbol,
                market_dir=meta.market_dir,
                market_code=meta.lean_market_code,
                front_month_expiry_yyyymm=front_month,
                bars=bars,
                open_interest=open_interest,
            )
            return MarketSyncResult(
                market=market_key,
                success=True,
                bars_written=len(bars),
                last_session_date=bars[-1].session_date,
                front_month_expiry=front_month,
                error=None,
                open_interest=open_interest,
            )
    except TimeoutError as exc:
        return MarketSyncResult(
            market=market_key,
            success=False,
            bars_written=0,
            last_session_date=None,
            front_month_expiry=None,
            error=f"ibkr_call_timeout:{exc!s}",
        )
    except Exception as exc:
        return MarketSyncResult(
            market=market_key,
            success=False,
            bars_written=0,
            last_session_date=None,
            front_month_expiry=None,
            error=f"{type(exc).__name__}:{exc!s}",
        )


# ---------------------------------------------------------------------------
# Long-lived worker
# ---------------------------------------------------------------------------


IbFactory = Callable[[], Any]
"""ib-async ``IB`` class (or test fake). Production uses ``ib_async.IB``."""


class BarSyncWorker:
    """Long-lived task that fires the bar-sync cycle once per ET calendar day.

    Lifecycle mirrors :class:`services.reconciliation.scheduler.ReconciliationScheduler`:

        worker = BarSyncWorker(config=cfg, ...)
        await worker.run_forever()  # blocks until request_stop()
        worker.request_stop()

    Per-cycle work:
      1. Connect a fresh ib-async ``IB`` to ``ib_gateway`` on ``clientId=2``.
      2. For each market in ``config.markets``, call :func:`sync_one_market`.
      3. Disconnect (best-effort).
      4. Log a single structured ``bar_sync_cycle_completed`` line.

    Per-market exceptions are captured in :class:`MarketSyncResult` —
    one market's failure doesn't abort the cycle. A connect failure
    at step 1 aborts the cycle (logs ``bar_sync_cycle_connect_failed``)
    and the next session day's tick retries.

    Tests inject ``ib_factory`` for a fake IB + ``clock`` for a fake
    UTC datetime stream.
    """

    def __init__(
        self,
        *,
        config: BarSyncConfig,
        ib_factory: IbFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        initial_fired_date: date | None = None,
    ) -> None:
        self._config = config
        if ib_factory is None:
            # Lazy import so tests + dev hosts without ib_async installed
            # can still import this module.
            def _default_factory() -> Any:  # pragma: no cover — exercised in production only
                from ib_async import IB as IbAsyncIB

                return IbAsyncIB()

            self._ib_factory = _default_factory
        else:
            self._ib_factory = ib_factory
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._last_fired_session_date_et: date | None = initial_fired_date
        self._stop_event = asyncio.Event()
        self._is_running = False
        self._log = log.bind(
            worker="bar_sync",
            sync_time_et=config.sync_time_et.isoformat(),
            ibkr_host=config.ibkr_host,
            ibkr_port=config.ibkr_port,
            ibkr_client_id=config.ibkr_client_id,
            data_root=str(config.data_root),
            bars_per_fetch=config.bars_per_fetch,
            markets_count=len(config.markets),
        )

    def request_stop(self) -> None:
        """Signal :meth:`run_forever` to exit at the next iteration boundary."""
        self._stop_event.set()

    @property
    def last_fired_session_date_et(self) -> date | None:
        """Most recent ET session date the worker fired (None if never)."""
        return self._last_fired_session_date_et

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def maybe_fire(self) -> BarSyncCycleResult | None:
        """If the schedule says fire-now, run the cycle. Otherwise no-op.

        Returns the cycle result if a cycle ran, else None. Mirrors
        ``ReconciliationScheduler.maybe_fire``.
        """
        now_utc = self._clock()
        if not should_fire_now(
            now_utc=now_utc,
            sync_time_et=self._config.sync_time_et,
            last_fired_session_date_et=self._last_fired_session_date_et,
        ):
            return None
        session_date = current_session_date_et(now_utc)
        self._log.info("bar_sync_cycle_firing", session_date_et=session_date.isoformat())
        result = await self.run_cycle(today=session_date)
        # Mark fired AFTER the cycle returns (regardless of success) so a
        # transient failure doesn't trigger a retry storm on the next tick.
        self._last_fired_session_date_et = session_date
        return result

    async def run_cycle(self, *, today: date | None = None) -> BarSyncCycleResult:
        """Run one bar-sync cycle (connect → fetch+write per market → disconnect).

        Returns a :class:`BarSyncCycleResult` regardless of partial
        failures; the caller inspects ``successful_markets`` /
        ``failed_markets``. Raises only on truly unrecoverable conditions
        (e.g., an exception inside the connect path that we didn't
        anticipate).

        ``today`` defaults to the ET calendar date derived from the
        injected clock so the cycle uses the same wall-clock anchor
        :meth:`maybe_fire` checked against.
        """
        cycle_started = self._clock()
        if today is None:
            today = current_session_date_et(cycle_started)
        ib = self._ib_factory()
        successful: list[MarketSyncResult] = []
        failed: list[MarketSyncResult] = []
        try:
            try:
                await asyncio.wait_for(
                    ib.connectAsync(
                        host=self._config.ibkr_host,
                        port=self._config.ibkr_port,
                        clientId=self._config.ibkr_client_id,
                        timeout=self._config.ibkr_connect_timeout_seconds,
                    ),
                    timeout=self._config.ibkr_connect_timeout_seconds + 5.0,
                )
                self._log.info(
                    "bar_sync_ibkr_connected",
                    session_date_et=today.isoformat(),
                )
            except Exception as exc:
                self._log.exception(
                    "bar_sync_cycle_connect_failed",
                    session_date_et=today.isoformat(),
                    error=str(exc),
                    exception_type=type(exc).__name__,
                )
                # Synthesize per-market failure rows so the cycle result
                # still reflects the full universe (helps the operator
                # see "11 markets all failed" vs. a partial outage).
                for market_key in self._config.markets:
                    failed.append(
                        MarketSyncResult(
                            market=market_key,
                            success=False,
                            bars_written=0,
                            last_session_date=None,
                            front_month_expiry=None,
                            error=f"ibkr_connect_failed:{type(exc).__name__}",
                        )
                    )
                cycle_completed = self._clock()
                return BarSyncCycleResult(
                    cycle_started_at_utc=cycle_started,
                    cycle_completed_at_utc=cycle_completed,
                    successful_markets=tuple(successful),
                    failed_markets=tuple(failed),
                )
            # Iterate markets serially — IBKR rate-limits aggressive
            # concurrent reqHistoricalData (max ~50 simultaneous; our 11
            # would fit but serial keeps log lines clean + makes timeout
            # debugging easier).
            for market_key, meta in self._config.markets.items():
                self._log.info(
                    "bar_sync_market_starting",
                    market=market_key,
                    kind=meta.kind,
                )
                result = await sync_one_market(
                    ib,
                    market_key,
                    meta,
                    config=self._config,
                    today=today,
                )
                if result.success:
                    successful.append(result)
                    self._log.info(
                        "bar_sync_market_completed",
                        market=market_key,
                        bars_written=result.bars_written,
                        last_session_date=(
                            result.last_session_date.isoformat()
                            if result.last_session_date
                            else None
                        ),
                        front_month_expiry=result.front_month_expiry,
                    )
                else:
                    failed.append(result)
                    self._log.warning(
                        "bar_sync_market_failed",
                        market=market_key,
                        error=result.error,
                    )
        finally:
            # Best-effort disconnect. Wrap in try/except because a
            # connect-failed cycle has ib in an indeterminate state.
            try:
                disconnect = getattr(ib, "disconnect", None)
                if disconnect is not None:
                    maybe_coro = disconnect()
                    if asyncio.iscoroutine(maybe_coro):
                        await asyncio.wait_for(maybe_coro, timeout=10.0)
            except Exception:
                self._log.exception("bar_sync_disconnect_failed")
        cycle_completed = self._clock()
        cycle_result = BarSyncCycleResult(
            cycle_started_at_utc=cycle_started,
            cycle_completed_at_utc=cycle_completed,
            successful_markets=tuple(successful),
            failed_markets=tuple(failed),
        )
        self._log.info(
            "bar_sync_cycle_completed",
            session_date_et=today.isoformat(),
            successful_count=len(successful),
            failed_count=len(failed),
            total_markets=cycle_result.total_markets,
            duration_seconds=round(
                (cycle_completed - cycle_started).total_seconds(),
                2,
            ),
            failed_markets=[r.market for r in failed],
        )
        return cycle_result

    async def run_forever(self) -> None:
        """Supervisor: tick + maybe_fire loop until ``request_stop``.

        Mirrors :meth:`ReconciliationScheduler.run_forever`. Per-tick
        exceptions are logged + swallowed so a transient bug doesn't
        kill the worker.
        """
        self._is_running = True
        self._log.info("bar_sync_worker_started")
        try:
            while not self._stop_event.is_set():
                try:
                    await self.maybe_fire()
                except Exception:
                    self._log.exception("bar_sync_worker_tick_error")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.tick_interval_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            self._is_running = False
            self._log.info("bar_sync_worker_stopped")


__all__ = [
    "DEFAULT_BARS_PER_FETCH",
    "DEFAULT_BAR_SYNC_CLIENT_ID",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_IBKR_CALL_TIMEOUT_SECONDS",
    "DEFAULT_OI_WAIT_SECONDS",
    "DEFAULT_SYNC_TIME_ET",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "PHASE1_UNIVERSE_METADATA",
    "Bar",
    "BarSyncConfig",
    "BarSyncCycleResult",
    "BarSyncWorker",
    "HistoricalDataFetcher",
    "IbFactory",
    "MarketMeta",
    "MarketSyncResult",
    "build_equity_daily_csv",
    "build_equity_factor_file",
    "build_equity_map_file",
    "build_futures_map_file",
    "build_futures_oi_csv",
    "build_futures_trade_csv",
    "build_futures_universe_csv",
    "current_session_date_et",
    "equity_daily_zip_path",
    "equity_factor_file_path",
    "equity_map_file_path",
    "fetch_etf_bars",
    "fetch_front_month_open_interest",
    "fetch_futures_bars_and_front_month",
    "futures_map_file_path",
    "futures_oi_zip_path",
    "futures_trade_zip_path",
    "futures_universe_file_path",
    "parse_ibkr_bars",
    "pick_front_month_expiry",
    "should_fire_now",
    "sync_one_market",
    "write_etf_bundle",
    "write_futures_bundle",
    "write_zip_with_member",
]
