"""services/data/bar_sync.py — daily bar sync from IBKR → LEAN on-disk format.

Option C of the 2026-05-20 data-layer pivot v2 (see
``Docs/decisions-log.md`` 2026-05-20 evening entry "Data-layer pivot
deploy ceremony: 3 sequential failure modes; lean_local stopped
pending v2 architecture"). The api owns the bar-sync responsibility:
fetches daily OHLCV bars for the Phase 1 universe from IBKR via a
dedicated ``ib-async`` connection on ``clientId=3`` (distinct from the
order-placement worker), writes them to the shared
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

* **clientId allocation** (per dev-guide §1.5 LOCKED — synced to
  deploy reality 2026-05-21):
    - ``clientId=1`` — api order-placement worker (services/execution/ibkr_adapter.py)
    - ``clientId=3`` — api bar_sync worker (this module)
    - ``clientId=80-99`` — operator probes + recovery tools
    - reserve ``4-7`` for future expansion (``clientId=2`` reserved
      for the order-placement worker's deploy override; see the
      decisions-log 2026-05-21 follow-up entry)

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
  EfficiencyRatio/ATR all derive from close prices, the continuous-mapped
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
import os
import tempfile
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

#: Default clientId for the bar_sync worker — synced to deploy
#: reality 2026-05-21 (was ``2`` prior to PR #210). The order-
#: placement worker's deploy override reserved clientId=2; bar_sync
#: ships on clientId=3 to avoid the collision that would otherwise
#: raise IBKR Error 162. See dev-guide §1.5 LOCKED + decisions-log
#: 2026-05-21 follow-up entry.
DEFAULT_BAR_SYNC_CLIENT_ID: Final[int] = 3

#: Default wall-clock budget for the front-month OI snapshot poll
#: (per futures market). IBKR's market-data ticks for futures
#: generic-tick 588 typically arrive within 1-2s of subscription;
#: 5s gives comfortable headroom without lengthening the cycle
#: materially. Set to 0 (or negative) on the BarSyncConfig to disable
#: the OI fetch entirely (universe files will then have empty OI
#: columns, equivalent to pre-2026-05-20 sub-pivot behavior).
DEFAULT_OI_WAIT_SECONDS: Final[float] = 5.0

#: Default sentinel value substituted into a futures universe file's
#: open-interest column when :func:`fetch_front_month_open_interest`
#: returns 0 (data-entitlement gap or transient failure). LEAN's
#: ``DataMappingMode.OPEN_INTEREST`` resolver picks "the contract with
#: ``OI > 0``"; an empty OI column causes the resolver to skip the
#: market entirely. The v1 strategy doesn't consume OI numerically —
#: only LEAN's contract-picker does — so substituting a small
#: positive sentinel keeps the market tradeable without affecting
#: strategy logic.
#:
#: Operator-approved follow-up (option (c)) to the 2026-05-21 OI saga:
#: /MCL's paper-account NYMEX delayed feed publishes OI=0.0 (not NaN)
#: because the entitlement gap returns a 0-sentinel rather than no
#: tick at all. Without substitution /MCL would remain ``v1_history_
#: unavailable`` until the operator upgrades the IBKR subscription or
#: drops the market from the Phase 1 universe. With substitution the
#: market trades cleanly using the api's continuous-mapped back-history.
#:
#: Set the corresponding :attr:`BarSyncConfig.oi_sentinel_when_fetch_failed`
#: to ``0`` to disable the substitution (preserves pre-2026-05-21
#: behavior where /MCL's universe file gets an empty OI column).
#:
#: See ``Docs/decisions-log.md`` 2026-05-21 entry "bar_sync OI fetch
#: saga" + the same-day follow-up Task 2 entry.
SENTINEL_OI_WHEN_FETCH_FAILED: Final[int] = 1

#: IBKR generic-tick ID that triggers ``futuresOpenInterest`` population
#: on the returned :class:`ib_async.Ticker`. Verified against
#: ``ib_async/ib.py::reqMktData`` docstring + ``ib_async/wrapper.py``
#: tick-type map ``86: 'futuresOpenInterest'``.
_IBKR_FUTURES_OI_GENERIC_TICK: Final[str] = "588"

#: Default count of daily bars fetched per historical-contract backfill call.
#: Each futures contract typically has ~9 months (~190 trading days) of
#: liquid history before expiry. 365 calendar days = ~260 trading days,
#: comfortably above the per-contract envelope so the union across rolls
#: covers the strategy's MA_SLOW_DAYS=200 requirement. The actual fetched
#: count is capped by IBKR's per-contract data depth (it serves what it
#: has — typically all of the contract's lifetime).
DEFAULT_HISTORICAL_BACKFILL_BARS: Final[int] = 365


#: IBKR market-data-type code applied to the bar_sync connection
#: before any ``reqMktData`` call. ``3`` = DELAYED (15-20 min lag);
#: works for any IBKR account without a real-time live-data
#: subscription. The operator's paper-trading account has NO
#: real-time futures-OI entitlement (verified by IBKR Error 354 on
#: 2026-05-21 00:25 UTC: "Requested market data is not subscribed.
#: ... Delayed market data is available."). For futures OI which
#: settles once per day, the 15-20 min lag on the delayed feed is
#: immaterial.
#:
#: Other valid values (set via ``BarSyncConfig.oi_market_data_type``):
#:   * 1 = LIVE — operator with paid real-time futures subscription
#:   * 2 = FROZEN — last live value during market closure
#:   * 4 = DELAYED_FROZEN — last delayed value during market closure
#:
#: See https://interactivebrokers.github.io/tws-api/market_data_type.html.
DEFAULT_OI_MARKET_DATA_TYPE: Final[int] = 3


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
    # convention (cme/comex/nymex/cbot). /MYM lives on cbot per LEAN's
    # FuturesExpiryFunctions.cs::MicroDow30EMini registration under
    # Market.CBOT + MHDB ``Future-cbot-MYM`` entry; the other 5
    # CME-listed index/crypto micros land on the cme directory.
    "/MES": MarketMeta("futures", "MES", "CME", "cme", "CME"),
    "/MNQ": MarketMeta("futures", "MNQ", "CME", "cme", "CME"),
    # /MYM market_dir + lean_market_code flipped from cme → cbot
    # 2026-05-23 (PR #226). Empirical confirmation from the 2026-05-22
    # PR #225 deploy: 6/7 futures resolved cleanly with MapFile.Count
    # ≥ 8 but /MYM stayed at MapFile.Count: 0 because the synthesized
    # map_file landed at ``future/cme/map_files/mym.csv`` while LEAN's
    # resolver looked at ``future/cbot/map_files/mym.csv``. See
    # ``Docs/decisions-log.md`` 2026-05-23 entry "/MYM market routing
    # follows LEAN CBOT registration" for the operator-side data
    # migration ceremony (one-time ``mv`` of existing /MYM universe +
    # daily zip data from cme/ to cbot/ on the VPS).
    "/MYM": MarketMeta("futures", "MYM", "CBOT", "cbot", "CBOT"),
    "/M2K": MarketMeta("futures", "M2K", "CME", "cme", "CME"),
    "/MGC": MarketMeta("futures", "MGC", "COMEX", "comex", "COMEX"),
    # /MCL sidelined 2026-05-23 (PR #228) — IBKR paper-tier NYMEX
    # entitlement gap. The sentinel-substitution + alert-builder code
    # paths in this module remain in place + are exercised by
    # ``tests/unit/test_bar_sync.py`` via the
    # ``_MCL_TEST_FIXTURE_META`` constant so they don't bit-rot.
    # See ``strategies/v1_trend_following/parameters.py``
    # ``V1_SIDELINED_MARKETS`` for the canonical sideline registry +
    # the re-enable runbook + ``Docs/decisions-log.md`` 2026-05-23
    # entry "/MCL sidelined from Phase 1 universe" for the full saga.
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
    syncs; ``0`` when the OI fetch ran AND substitution is disabled
    via :attr:`BarSyncConfig.oi_sentinel_when_fetch_failed=0`;
    :data:`SENTINEL_OI_WHEN_FETCH_FAILED` (typically ``1``) when the
    fetch returned 0 and substitution fired; a positive int distinct
    from the sentinel when LEAN's resolver gets a real value).

    ``open_interest_was_sentinel`` is ``True`` only when the futures
    OI fetch returned 0 AND the sentinel substitution path fired
    (``open_interest`` then equals the configured sentinel). Used by
    :mod:`services.data.bar_sync_alerts` to detect the persistent
    data-entitlement gap pattern (e.g., /MCL on a paper-tier NYMEX
    subscription) for the P2 alert builder. Always ``False`` for ETFs
    (no OI) and for failed syncs.
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
    open_interest_was_sentinel: bool = False  # True only when substitution fired in sync_one_market


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
            parameters. Default clientId=3 per dev-guide §1.5 LOCKED
            (synced to deploy reality 2026-05-21).
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
    #: IBKR market-data-type code (1=LIVE, 2=FROZEN, 3=DELAYED,
    #: 4=DELAYED_FROZEN). Default 3 (DELAYED) — the operator's paper
    #: account has no real-time futures-OI entitlement. Override to 1
    #: if/when a live subscription is acquired.
    oi_market_data_type: int = DEFAULT_OI_MARKET_DATA_TYPE
    #: Sentinel OI value substituted into the universe file when
    #: ``fetch_front_month_open_interest`` returns 0 (data-entitlement
    #: gap or transient failure). Default ``1`` keeps markets with
    #: published-but-unentitled OI (e.g., /MCL on a paper-tier NYMEX
    #: subscription) tradeable; set to ``0`` to disable substitution
    #: and preserve the pre-2026-05-21 empty-OI-column behavior.
    oi_sentinel_when_fetch_failed: int = SENTINEL_OI_WHEN_FETCH_FAILED
    #: Minimum consecutive sessions a new front-month expiry must
    #: persist before the map_file synthesizer counts it as a genuine
    #: roll boundary. Default 15 (≈3 weeks; operator-recommended in
    #: the 2026-05-22 brief). Lower values admit more noise; higher
    #: values risk missing real rolls during illiquid stretches.
    map_file_persistence_days: int = 15
    #: When True, ``BarSyncWorker.run_cycle`` calls
    #: :func:`services.data.map_file_synthesis.synthesize_futures_map_file`
    #: for each futures market after the per-market sync loop. Set False
    #: in tests that only exercise the sync path; default True per the
    #: 2026-05-22 decisions-log entry "Diagnostic probe (PR #220)
    #: CONFIRMS root cause: LEAN's continuous-contract resolver returns
    #: empty DataFrame for futures".
    enable_map_file_synthesis: bool = True
    #: When True, ``BarSyncWorker.run_cycle`` calls
    #: :func:`backfill_historical_contracts_for_ticker` for each futures
    #: market AFTER the per-market sync loop + BEFORE map_file synthesis.
    #: Adds per-historical-contract daily CSVs to each ticker's trade
    #: zip so LEAN's continuous-contract resolver can stitch ≥ 200 bars
    #: from the roll history (the strategy's ``MA_SLOW_DAYS=200`` floor).
    #: Set False in tests that only exercise the front-month sync path;
    #: default True per the 2026-05-23 decisions-log entry "Historical
    #: contract backfill unblocks MA_SLOW=200".
    enable_historical_contract_backfill: bool = True
    #: Count of daily bars requested per historical-contract backfill
    #: call (``durationStr=f"{N} D"``). 365 calendar days = ~260 trading
    #: days; covers a typical 9-month futures contract lifetime with
    #: headroom. Smaller values fetch faster but may under-serve LEAN's
    #: per-contract resolver if a contract's actual lifetime exceeds
    #: the request.
    historical_backfill_bars_per_contract: int = DEFAULT_HISTORICAL_BACKFILL_BARS


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
    The strategy operates on un-adjusted prices (Donchian/MA/EfficiencyRatio/ATR
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


def list_zip_member_names(zip_path: Path) -> set[str]:
    """Return the set of member filenames in ``zip_path``.

    Returns empty set if the file doesn't exist or is unreadable as a
    zip (the caller treats both as "no members present"). Used by the
    historical-contract backfill path to short-circuit IBKR fetches
    when the per-contract CSV is already on disk.
    """
    if not zip_path.is_file():
        return set()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return set(zf.namelist())
    except (zipfile.BadZipFile, OSError):  # pragma: no cover — defensive
        return set()


def write_zip_with_members_preserving(zip_path: Path, members_to_write: dict[str, bytes]) -> int:
    """Add/replace ``members_to_write`` in ``zip_path``; preserve other members.

    Atomic via tmp-file + ``os.replace`` (single-syscall rename on the
    same filesystem; mirrors the pattern in
    :mod:`services.data.map_file_synthesis._write_atomic`).

    Existing members whose names appear in ``members_to_write`` are
    replaced with the new bytes; other existing members are preserved
    byte-for-byte. If the file doesn't exist a fresh zip is created
    containing only ``members_to_write``.

    This is the load-bearing primitive for the historical-contract
    backfill (2026-05-23+): :func:`write_futures_bundle` writes the
    current front-month here so prior cycles' backfilled historical
    CSVs survive the rewrite; :func:`backfill_historical_contracts_for_ticker`
    writes the newly-fetched historical CSVs in a single rewrite so the
    zip stays consistent on partial failure (atomic).

    Returns the resulting file's size in bytes. Raises ``OSError`` if
    the tmp-file or replace fails — the caller's error policy is
    per-call (the backfill orchestrator catches + logs per ticker).
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    # Snapshot existing members (excluding any that we're about to overwrite).
    existing: dict[str, bytes] = {}
    if zip_path.is_file():
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    if name in members_to_write:
                        continue
                    existing[name] = zf.read(name)
        except (zipfile.BadZipFile, OSError):  # pragma: no cover — defensive
            # Existing zip is unreadable — start fresh.
            existing = {}
    # tempfile.NamedTemporaryFile on the same dir guarantees ``os.replace``
    # is atomic (single-filesystem rename); ``delete=False`` so the close
    # in our finally block doesn't auto-delete before the rename.
    tmp = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=str(zip_path.parent),
        prefix=f".{zip_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.close()  # close the handle so ZipFile can open the path fresh
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            # Existing members first in sorted order (deterministic on-disk
            # layout simplifies diffs / debugging) then the new/updated ones.
            for name in sorted(existing):
                zf.writestr(name, existing[name])
            for name in sorted(members_to_write):
                zf.writestr(name, members_to_write[name])
        os.replace(tmp.name, zip_path)
    except Exception:
        # Best-effort cleanup of the tmp file on any failure.
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
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


def _per_bar_front_month_or_fallback(*, ticker: str, session_date: date, fallback: str) -> str:
    """Compute the historical front-month for ``ticker`` on ``session_date``.

    Delegates to :func:`map_file_synthesis.front_month_for_session_date`
    which walks the ticker's cycle months + uses the existing per-ticker
    expiry rules to find the smallest contract whose expiry is >=
    ``session_date``.

    If the lookup raises (unknown ticker, no expiry rule registered, no
    candidate within 48 cycle iterations), falls back to the caller-
    supplied ``fallback`` (typically today's pick). The fallback path
    preserves the pre-2026-05-24 behavior — same expiry for all bars —
    rather than crashing the per-bar loop on a stale or misconfigured
    ticker. The fallback only fires for code paths we don't yet support
    (e.g., a new ticker added to ``PHASE1_UNIVERSE_METADATA`` without a
    matching entry in ``_EXPIRY_RULES``).

    Lazy import of :mod:`map_file_synthesis` matches the existing pattern
    elsewhere in this module (avoids any startup-order coupling between
    bar_sync's module-level state + the synthesizer).
    """
    try:
        from services.data.map_file_synthesis import (
            front_month_for_session_date as _front_month,
        )

        return _front_month(ticker=ticker, session_date=session_date)
    except (ValueError, RuntimeError) as exc:
        log.warning(
            "per_bar_front_month_fallback",
            ticker=ticker,
            session_date=session_date.isoformat(),
            fallback=fallback,
            error=str(exc),
        )
        return fallback


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

    The trade-zip + OI-zip writes go through
    :func:`write_zip_with_members_preserving` so prior cycles'
    backfilled historical-contract CSVs survive the per-cycle rewrite
    (added 2026-05-23 for the historical-contract backfill follow-up
    PR; see ``Docs/decisions-log.md`` 2026-05-23 entry "Historical
    contract backfill unblocks MA_SLOW=200"). Without the
    preserving-write, ``sync_one_market`` would wipe the historical
    CSVs the backfill phase wrote each cycle, forcing re-fetch of every
    historical contract on every cycle (~60-90 IBKR calls / cycle vs.
    ~0 in steady state).

    Returns the trade-zip's size in bytes. Raises ``ValueError`` if
    ``bars`` is empty.
    """
    if not bars:
        raise ValueError(f"write_futures_bundle: bars is empty for {ticker!r}")
    trade_csv_bytes = build_futures_trade_csv(bars)
    trade_zip = futures_trade_zip_path(data_root, ticker, market_dir)
    trade_member = f"{ticker.lower()}_trade_{front_month_expiry_yyyymm}.csv"
    trade_size = write_zip_with_members_preserving(trade_zip, {trade_member: trade_csv_bytes})
    oi_csv_bytes = build_futures_oi_csv(bars, open_interest)
    oi_zip = futures_oi_zip_path(data_root, ticker, market_dir)
    oi_member = f"{ticker.lower()}_openinterest_{front_month_expiry_yyyymm}.csv"
    write_zip_with_members_preserving(oi_zip, {oi_member: oi_csv_bytes})
    # Per-day universe files — one per session_date in bars.
    #
    # Each universe file's first-column `expiry` is the front-month for
    # THAT bar's session_date (NOT today's front-month). This is the
    # 2026-05-24 forward-fix for the per-bar universe-write bug surfaced
    # by the 21:30 UTC LEAN cycle's depth-shortfall diagnosis: pre-fix,
    # this loop wrote `front_month_expiry_yyyymm` (today's pick) into
    # EVERY universe file in the 175-bar window, corrupting the
    # synthesizer's roll-detection input + producing the /MES map_file's
    # 9-month gap between rolls 2025-06-22 (`yv` -> 202509) and 2026-03-16
    # (`z3` -> 202606). See ``Docs/decisions-log.md`` 2026-05-24 entry
    # "bar_sync per-bar front-month write" for the full diagnostic chain.
    #
    # The per-bar expiry is computed deterministically from the bar's
    # session_date via :func:`map_file_synthesis.front_month_for_session_date`
    # which uses the existing per-ticker expiry rules (third-Friday for
    # index quarterlies; 3rd-last business day for /MGC; etc.). No IBKR
    # call per bar.
    universe_oi = open_interest if open_interest > 0 else None
    universe_permission_skips: list[str] = []
    for bar in bars:
        u_path = futures_universe_file_path(data_root, ticker, market_dir, bar.session_date)
        u_path.parent.mkdir(parents=True, exist_ok=True)
        bar_front_month = _per_bar_front_month_or_fallback(
            ticker=ticker,
            session_date=bar.session_date,
            fallback=front_month_expiry_yyyymm,
        )
        u_body = build_futures_universe_csv(bar_front_month, bar, universe_oi)
        try:
            u_path.write_bytes(u_body)
        except PermissionError as exc:
            # Resilience (2026-05-30): a single un-writable per-day universe
            # file must NOT fail the whole market. This fires when a prior
            # bar_sync run (executing as root, before the container switched to
            # uid 1000) left a root-owned `-rw-r--r--` file for THAT session
            # date — uid 1000 then can't rewrite it (EACCES). The trade + OI
            # zips for this market have ALREADY been written above, and the
            # per-day universe file already on disk holds the same content
            # (the front-month + OHLCV for that historical session don't
            # change), so skipping the rewrite is safe — LEAN reads the
            # existing file. The systemic remedy is the one-time volume chown
            # to uid 1000 (deploy/ops); this guard stops the daily cycle from
            # marking the market failed until that lands + survives any future
            # ownership drift. See decisions-log 2026-05-30.
            universe_permission_skips.append(f"{bar.session_date:%Y%m%d}")
            log.warning(
                "bar_sync_universe_write_permission_skipped",
                ticker=ticker,
                market_dir=market_dir,
                session_date=f"{bar.session_date:%Y%m%d}",
                path=str(u_path),
                error=f"{type(exc).__name__}: {exc}",
            )
    if universe_permission_skips:
        log.warning(
            "bar_sync_universe_write_permission_summary",
            ticker=ticker,
            market_dir=market_dir,
            skipped_count=len(universe_permission_skips),
            total_bars=len(bars),
            skipped_session_dates=universe_permission_skips,
            hint=(
                "Per-day universe rewrites were skipped due to PermissionError "
                "(root-owned files from a pre-uid-1000 bar_sync run). The "
                "existing on-disk files are reused. Remediate with a one-time "
                "chown -R 1000:1000 /Lean/Data on the host volume."
            ),
        )
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
      within the same TWS session. Cycling clientId=3 connect/
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
# Historical-contract backfill (added 2026-05-23 — bars_per_fetch=250 D against
# ContFuture only returns the current front-month's ~177 trading days because
# IBKR's ContFuture does NOT splice in bars from prior expiries; it serves
# the current front-month contract's full history. MA_SLOW_DAYS=200 needs
# ≥ 200 bars per market for entry signals to emit; without per-historical-
# contract CSVs the strategy rejects every market on the WARMUP_TREND
# filter. See ``Docs/decisions-log.md`` 2026-05-23 entry.)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalContractBackfillResult:
    """Outcome of one ticker's historical-contract backfill pass.

    Returned by :func:`backfill_historical_contracts_for_ticker`. Aggregated
    per-cycle into the structured log lines so the operator can grep
    ``historical_contracts_backfill_completed_for_ticker`` to see the
    per-ticker count of newly-fetched contracts.

    ``backfilled_expiries`` lists the YYYYMM strings whose CSVs were
    fetched + written this call; ``skipped_expiries`` lists the YYYYMM
    strings that were already on disk (idempotency); ``failed_expiries``
    lists the YYYYMM strings whose IBKR fetch raised or returned no
    bars (the cycle continues; the cumulative on-disk state degrades
    gracefully — strategy reads what's there, drops what isn't).
    """

    ticker: str
    market_dir: str
    backfilled_expiries: tuple[str, ...]
    skipped_expiries: tuple[str, ...]
    failed_expiries: tuple[str, ...]
    bars_fetched_total: int


async def fetch_historical_contract_bars(
    ib: Any,
    market_key: str,
    *,
    meta: MarketMeta,
    contract_month: str,
    expiry_date: date,
    bars_count: int,
    call_timeout_seconds: float,
) -> list[Bar]:
    """Fetch daily bars for ONE specific futures contract month.

    Builds a non-continuous :class:`ib_async.Future` with
    ``lastTradeDateOrContractMonth=contract_month`` + ``includeExpired=True``
    so IBKR resolves both currently-trading + expired contracts.
    Qualifies the contract via :meth:`IB.qualifyContractsAsync` (populates
    ``conId`` in-place; required for ``reqHistoricalData`` on expired
    contracts) then issues ``reqHistoricalDataAsync`` with
    ``endDateTime`` pinned to the contract's expiry date so IBKR returns
    bars up to that contract's last trading day (not "up to now" —
    that would return no bars for an expired contract).

    Returns the parsed :class:`Bar` list (sorted ascending by
    ``session_date`` + de-duped). Returns ``[]`` on any failure mode
    (qualification raises / returns no match, ``reqHistoricalData``
    times out, IBKR returns no bars). Per-failure-mode logging is at
    WARNING; the caller treats an empty list as "skip this contract".

    Used by :func:`backfill_historical_contracts_for_ticker` for each
    historical roll boundary in the synthesizer's per-ticker rolls.

    Args:
        ib: ib-async ``IB`` instance (or test fake).
        market_key: ``/MES``-style key for logging.
        meta: :class:`MarketMeta` — must be ``kind="futures"``.
        contract_month: 6-digit ``YYYYMM`` (e.g., ``"202403"``).
        expiry_date: actual last-trading day for ``contract_month``;
            used as ``endDateTime``. Compute via
            :func:`services.data.map_file_synthesis.compute_future_expiry`.
        bars_count: count of daily bars to request (passed to IBKR as
            ``durationStr=f"{N} D"``).
        call_timeout_seconds: per-call wall-clock budget for each
            ib-async await.

    Raises ``ValueError`` if ``meta.kind != "futures"``.
    """
    if meta.kind != "futures":
        raise ValueError(f"fetch_historical_contract_bars called with non-futures meta {meta!r}")
    from ib_async import Future

    bound_log = log.bind(
        op="fetch_historical_contract_bars",
        market=market_key,
        contract_month=contract_month,
        expiry_date=expiry_date.isoformat(),
    )

    contract: Any
    try:
        contract = Future(
            symbol=meta.ibkr_symbol,
            exchange=meta.ibkr_exchange,
            lastTradeDateOrContractMonth=contract_month,
            includeExpired=True,
        )
    except Exception:  # pragma: no cover — Future ctor is dataclass-like
        bound_log.exception("historical_contract_build_failed")
        return []

    qualify = getattr(ib, "qualifyContractsAsync", None)
    if qualify is None:
        bound_log.warning("historical_contract_qualify_unavailable")
        return []
    try:
        qualified = await asyncio.wait_for(qualify(contract), timeout=call_timeout_seconds)
    except TimeoutError:
        bound_log.warning(
            "historical_contract_qualify_timeout",
            call_timeout_seconds=call_timeout_seconds,
        )
        return []
    except Exception as exc:
        bound_log.warning(
            "historical_contract_qualify_failed",
            error=str(exc),
            exception_type=type(exc).__name__,
        )
        return []
    qualified_list = list(qualified or [])
    if not qualified_list or qualified_list[0] is None:
        bound_log.warning("historical_contract_qualify_returned_no_match")
        return []
    qualified_contract = qualified_list[0]

    # IBKR expects ``YYYYMMDD HH:MM:SS`` format on ``endDateTime``; an
    # empty string would default to "now" and return zero bars for an
    # expired contract. Anchor to the contract's last trading day at
    # end-of-day so IBKR includes bars up to (and including) expiry.
    end_dt_str = f"{expiry_date:%Y%m%d} 23:59:59"
    duration = f"{bars_count} D"
    try:
        bar_datas = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                qualified_contract,
                endDateTime=end_dt_str,
                durationStr=duration,
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
            ),
            timeout=call_timeout_seconds,
        )
    except TimeoutError:
        bound_log.warning(
            "historical_contract_reqhistorical_timeout",
            call_timeout_seconds=call_timeout_seconds,
        )
        return []
    except Exception as exc:
        bound_log.warning(
            "historical_contract_reqhistorical_failed",
            error=str(exc),
            exception_type=type(exc).__name__,
        )
        return []

    bars = parse_ibkr_bars(bar_datas or [])
    bound_log.info("historical_contract_fetched", bars_count=len(bars))
    return bars


def historical_contract_months_from_disk(
    *,
    data_root: Path,
    meta: MarketMeta,
    persistence_days: int,
) -> tuple[list[str], dict[str, date]]:
    """Compute the historical-expiry set + per-expiry date map from disk.

    Reads the ticker's on-disk universe sessions via
    :func:`services.data.map_file_synthesis.iter_universe_sessions`
    + runs the persistence-filtered roll detector via
    :func:`services.data.map_file_synthesis.detect_real_rolls`. Returns
    the union of ``from_expiry`` + ``to_expiry`` across all detected
    rolls (sorted ascending) paired with a dict mapping each YYYYMM to
    its computed last-trading date (via
    :func:`services.data.map_file_synthesis.compute_future_expiry`).

    Returns ``([], {})`` when the universe directory doesn't exist
    yet OR no rolls are detected (single-expiry history). The caller
    short-circuits backfill in that case.

    A non-futures :class:`MarketMeta` raises ``ValueError`` — the
    caller filters before invoking.
    """
    if meta.kind != "futures":
        raise ValueError(
            f"historical_contract_months_from_disk called with non-futures meta {meta!r}"
        )
    # Local import to avoid the module-import-time circular dependency
    # with :mod:`map_file_synthesis` (mirrors the pattern in
    # :meth:`BarSyncWorker._synthesize_futures_map_files`).
    from services.data import map_file_synthesis as _mfs

    universe_dir = data_root / "future" / meta.market_dir / "universes" / meta.ibkr_symbol.lower()
    sessions = list(_mfs.iter_universe_sessions(universe_dir))
    rolls = _mfs.detect_real_rolls(sessions, persistence_days=persistence_days)
    expiries: set[str] = set()
    for roll in rolls:
        expiries.add(roll.from_expiry)
        expiries.add(roll.to_expiry)
    if not expiries:
        return [], {}
    expiry_dates: dict[str, date] = {}
    for yyyymm in expiries:
        try:
            expiry_dates[yyyymm] = _mfs.compute_future_expiry(
                ticker=meta.ibkr_symbol, contract_month=yyyymm
            )
        except ValueError:
            # Unknown ticker or malformed contract_month — skip rather
            # than fail the whole ticker's backfill.
            continue
    sorted_expiries = sorted(yyyymm for yyyymm in expiries if yyyymm in expiry_dates)
    return sorted_expiries, expiry_dates


async def backfill_historical_contracts_for_ticker(
    ib: Any,
    market_key: str,
    *,
    meta: MarketMeta,
    contract_months_to_ensure: Iterable[str],
    expiry_dates: dict[str, date],
    data_root: Path,
    bars_per_contract: int,
    call_timeout_seconds: float,
) -> HistoricalContractBackfillResult:
    """Backfill historical-contract daily CSVs into the ticker's trade zip.

    Idempotent: for each YYYYMM in ``contract_months_to_ensure``,
    short-circuits if the per-expiry CSV is already in the zip
    (cheap ``zipfile.namelist`` check, no IBKR call). Newly-fetched
    contracts are accumulated in memory then written via a single
    :func:`write_zip_with_members_preserving` call so the zip stays
    consistent on partial failure (atomic single-rename).

    Per-contract IBKR failures are caught + logged at WARNING; the
    backfill continues with subsequent expiries. The function NEVER
    raises (the worker treats backfill as best-effort observability;
    a failed backfill degrades to fewer historical bars, never a
    cycle-level abort).

    Args:
        ib: ib-async ``IB`` instance (or test fake).
        market_key: ``/MES``-style key for logging.
        meta: :class:`MarketMeta` — must be ``kind="futures"``.
        contract_months_to_ensure: YYYYMM strings to ensure present in
            the zip; typically the synthesizer's historical-expiry
            union via :func:`historical_contract_months_from_disk`.
        expiry_dates: per-YYYYMM last-trading date (paired with
            ``contract_months_to_ensure``); used as ``endDateTime`` on
            the IBKR fetch. Missing keys → that expiry skipped with
            warning log.
        data_root: LEAN on-disk root.
        bars_per_contract: count of daily bars to request per historical
            contract (``durationStr=f"{N} D"``).
        call_timeout_seconds: per-call wall-clock deadline on each
            ib-async await.

    Returns a :class:`HistoricalContractBackfillResult` summarizing the
    pass.
    """
    if meta.kind != "futures":
        raise ValueError(
            f"backfill_historical_contracts_for_ticker called with non-futures meta {meta!r}"
        )
    bound_log = log.bind(
        op="backfill_historical_contracts_for_ticker",
        market=market_key,
        ticker=meta.ibkr_symbol.lower(),
        market_dir=meta.market_dir,
    )
    months = tuple(contract_months_to_ensure)
    if not months:
        bound_log.info("historical_contracts_backfill_no_expiries")
        return HistoricalContractBackfillResult(
            ticker=meta.ibkr_symbol.lower(),
            market_dir=meta.market_dir,
            backfilled_expiries=(),
            skipped_expiries=(),
            failed_expiries=(),
            bars_fetched_total=0,
        )

    trade_zip = futures_trade_zip_path(data_root, meta.ibkr_symbol, meta.market_dir)
    existing_members = list_zip_member_names(trade_zip)
    new_members: dict[str, bytes] = {}
    backfilled: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    bars_total = 0
    for yyyymm in sorted(months):
        member_name = f"{meta.ibkr_symbol.lower()}_trade_{yyyymm}.csv"
        if member_name in existing_members:
            skipped.append(yyyymm)
            continue
        expiry_date = expiry_dates.get(yyyymm)
        if expiry_date is None:
            bound_log.warning(
                "historical_contracts_backfill_skip_no_expiry_date",
                contract_month=yyyymm,
            )
            failed.append(yyyymm)
            continue
        try:
            bars = await fetch_historical_contract_bars(
                ib,
                market_key,
                meta=meta,
                contract_month=yyyymm,
                expiry_date=expiry_date,
                bars_count=bars_per_contract,
                call_timeout_seconds=call_timeout_seconds,
            )
        except Exception as exc:
            bound_log.warning(
                "historical_contracts_backfill_fetch_unhandled_exception",
                contract_month=yyyymm,
                error=str(exc),
                exception_type=type(exc).__name__,
            )
            failed.append(yyyymm)
            continue
        if not bars:
            # fetch_historical_contract_bars already logged the specific
            # failure mode (timeout / no match / IBKR error). Treat as a
            # per-contract failure for the result summary.
            failed.append(yyyymm)
            continue
        new_members[member_name] = build_futures_trade_csv(bars)
        backfilled.append(yyyymm)
        bars_total += len(bars)

    if new_members:
        try:
            write_zip_with_members_preserving(trade_zip, new_members)
        except Exception:
            bound_log.exception(
                "historical_contracts_backfill_zip_write_failed",
                trade_zip=str(trade_zip),
                new_member_count=len(new_members),
            )
            # Demote successfully-fetched expiries from "backfilled" to
            # "failed" — they didn't actually land on disk this cycle.
            failed.extend(backfilled)
            backfilled = []
            bars_total = 0

    bound_log.info(
        "historical_contracts_backfill_completed_for_ticker",
        backfilled_count=len(backfilled),
        skipped_count=len(skipped),
        failed_count=len(failed),
        bars_fetched_total=bars_total,
        backfilled_expiries=backfilled,
        failed_expiries=failed,
    )
    return HistoricalContractBackfillResult(
        ticker=meta.ibkr_symbol.lower(),
        market_dir=meta.market_dir,
        backfilled_expiries=tuple(backfilled),
        skipped_expiries=tuple(skipped),
        failed_expiries=tuple(failed),
        bars_fetched_total=bars_total,
    )


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
            # the helper returns 0 on any error. When the helper
            # returns 0 AND the operator-configured sentinel is
            # positive, substitute the sentinel so LEAN's
            # DataMappingMode.OPEN_INTEREST resolver picks the contract
            # downstream (the v1 strategy doesn't consume OI
            # numerically; only LEAN's picker does). When the sentinel
            # is 0 the substitution is disabled and the bundle writer
            # treats the raw 0 as "OI unknown" (empty universe-file
            # column + zero-line OI zip; equivalent to pre-2026-05-21
            # behavior).
            raw_open_interest = await fetch_front_month_open_interest(
                ib,
                market_key,
                meta=meta,
                front_month_expiry_yyyymm=front_month,
                wait_seconds=config.oi_wait_seconds,
            )
            effective_open_interest = raw_open_interest
            sentinel_substituted = False
            if raw_open_interest <= 0 and config.oi_sentinel_when_fetch_failed > 0:
                effective_open_interest = config.oi_sentinel_when_fetch_failed
                sentinel_substituted = True
                log.warning(
                    "oi_sentinel_substituted",
                    op="sync_one_market",
                    market=market_key,
                    front_month_expiry=front_month,
                    reason="fetch_returned_zero",
                    raw_open_interest=raw_open_interest,
                    sentinel=effective_open_interest,
                )
            write_futures_bundle(
                data_root=config.data_root,
                ticker=meta.ibkr_symbol,
                market_dir=meta.market_dir,
                market_code=meta.lean_market_code,
                front_month_expiry_yyyymm=front_month,
                bars=bars,
                open_interest=effective_open_interest,
            )
            return MarketSyncResult(
                market=market_key,
                success=True,
                bars_written=len(bars),
                last_session_date=bars[-1].session_date,
                front_month_expiry=front_month,
                error=None,
                open_interest=effective_open_interest,
                open_interest_was_sentinel=sentinel_substituted,
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
      1. Connect a fresh ib-async ``IB`` to ``ib_gateway`` on ``clientId=3``.
      2. For each market in ``config.markets``, call :func:`sync_one_market`.
      3. Disconnect (best-effort).
      4. Log a single structured ``bar_sync_cycle_completed`` line.
      5. Evaluate P2 alerts (partial-cycle-failure + OI-sentinel-
         substitution) via :mod:`services.data.bar_sync_alerts`. Track
         consecutive-cycle counters; emit alerts at ``count >= 2``
         (avoids flap noise on transient single-cycle failures). A
         cycle that recovers fully resets both counters.

    Per-market exceptions are captured in :class:`MarketSyncResult` —
    one market's failure doesn't abort the cycle. A connect failure
    at step 1 aborts the cycle (logs ``bar_sync_cycle_connect_failed``)
    and the next session day's tick retries.

    Tests inject ``ib_factory`` for a fake IB + ``clock`` for a fake
    UTC datetime stream + optional ``partial_cycle_alert_hook`` to
    capture the descriptor without standing up a real Discord stack.
    """

    def __init__(
        self,
        *,
        config: BarSyncConfig,
        ib_factory: IbFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        initial_fired_date: date | None = None,
        partial_cycle_alert_hook: Any | None = None,
    ) -> None:
        # Local import to break a circular dependency at module-import
        # time — :mod:`bar_sync_alerts` imports from this module too.
        from services.data import bar_sync_alerts as _bsa

        self._bsa = _bsa
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
        self._partial_cycle_alert_hook = partial_cycle_alert_hook
        # Consecutive-cycle counters for the two P2 alert flavors. Reset
        # to 0 on a fully-clean cycle (no failures, no sentinels).
        self._consecutive_failure_count: int = 0
        self._consecutive_sentinel_count: int = 0
        # Most-recent cycle outcome, retained in memory for the read-only
        # ``GET /api/system/bar-sync`` status surface (mirrors the
        # heartbeat-registry precedent in ``services/api/heartbeats.py`` —
        # in-memory, not a DB table; lost on api restart, which the status
        # endpoint reports honestly as "no cycle since restart"). ``None``
        # until the first cycle fires.
        self._last_cycle_result: BarSyncCycleResult | None = None
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

    @property
    def last_cycle_result(self) -> BarSyncCycleResult | None:
        """Most-recent :class:`BarSyncCycleResult`, or None before the first cycle.

        Retained in memory for the read-only status surface; not persisted.
        After an api restart this reads None until the next cycle fires.
        """
        return self._last_cycle_result

    @property
    def consecutive_failure_count(self) -> int:
        """Consecutive cycles (incl. the latest) that had ≥1 failed market.

        Resets to 0 on a fully-clean cycle. Mirrors the partial-cycle
        alert counter; exposed read-only for the status surface.
        """
        return self._consecutive_failure_count

    @property
    def consecutive_sentinel_count(self) -> int:
        """Consecutive cycles (incl. the latest) that OI-sentinel-substituted.

        Resets to 0 on a cycle with no sentinel substitution. Exposed
        read-only for the status surface.
        """
        return self._consecutive_sentinel_count

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
                # Set the connection-wide market-data-type BEFORE any
                # downstream reqMktData call. IBKR Error 354 ("Requested
                # market data is not subscribed") fires on real-time
                # reqMktData when the account lacks the entitlement —
                # but DELAYED (15-20 min lag) is free for any account
                # holder. The operator's paper account has no real-time
                # futures-OI subscription (verified 2026-05-21 00:25 UTC
                # post-PR-#206 deploy). Effect is one-time per connection;
                # all subsequent reqMktData calls inherit this type.
                req_mdt = getattr(ib, "reqMarketDataType", None)
                if req_mdt is not None:
                    try:
                        req_mdt(self._config.oi_market_data_type)
                    except Exception:
                        # Best-effort: a failure here means the OI fetch
                        # will likely fail too (returning 0 → empty
                        # universe-file OI), but the cycle still writes
                        # the bundle. Log + continue.
                        self._log.exception(
                            "bar_sync_req_market_data_type_failed",
                            requested_market_data_type=self._config.oi_market_data_type,
                        )
                self._log.info(
                    "bar_sync_ibkr_connected",
                    session_date_et=today.isoformat(),
                    oi_market_data_type=self._config.oi_market_data_type,
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
                connect_failed_result = BarSyncCycleResult(
                    cycle_started_at_utc=cycle_started,
                    cycle_completed_at_utc=cycle_completed,
                    successful_markets=tuple(successful),
                    failed_markets=tuple(failed),
                )
                if self._config.enable_map_file_synthesis:
                    # Even on connect failure: the on-disk universe files
                    # from prior cycles are still valid inputs to the
                    # synthesizer. Idempotent — no-op if content matches
                    # what we last wrote. Lets the operator land the PR
                    # and have map_files populated immediately without
                    # waiting for a subsequent successful cycle.
                    self._synthesize_futures_map_files()
                await self._evaluate_alerts(connect_failed_result)
                return connect_failed_result
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
            # Historical-contract backfill runs INSIDE the connect-succeeded
            # arm of the try block — it needs a live ib connection. Runs
            # AFTER the per-market sync loop completes so the synthesizer's
            # rolls reflect the freshly-written universe files; runs BEFORE
            # the finally-block disconnect so the ib session is still
            # usable. Best-effort: per-ticker exceptions are caught + logged
            # by ``_backfill_historical_contracts`` so a backfill failure
            # never aborts the cycle.
            if self._config.enable_historical_contract_backfill:
                await self._backfill_historical_contracts(ib, today=today)
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
        # Retain for the read-only status surface. Set BEFORE alert
        # evaluation so the consecutive counters the endpoint reads are
        # consistent with this same result (``_evaluate_alerts`` updates
        # the counters below).
        self._last_cycle_result = cycle_result
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
        if self._config.enable_map_file_synthesis:
            # Map_file synthesis runs after the per-market loop completes
            # + before alert evaluation. See 2026-05-22 decisions-log
            # entry "Diagnostic probe (PR #220) CONFIRMS root cause" for
            # the fix's role: without populated map_files LEAN's
            # continuous-contract resolver returns empty DataFrames for
            # `self.history()` on every futures symbol.
            self._synthesize_futures_map_files()
        await self._evaluate_alerts(cycle_result)
        return cycle_result

    def _synthesize_futures_map_files(self) -> None:
        """Synthesize LEAN futures map_files from on-disk universe history.

        Walks the futures markets in :attr:`BarSyncConfig.markets` and
        calls :func:`services.data.map_file_synthesis.synthesize_futures_map_file`
        for each. Each call is wrapped in its own try/except so a
        single ticker's I/O failure doesn't abort the loop; the
        observability log line + a ``map_file_synthesis_failed`` event
        on exception suffice for the operator to grep.

        Triggered once per cycle AFTER the per-market sync loop
        (regardless of partial failures — the universe files from
        prior successful cycles remain on disk and are valid input).
        See the 2026-05-22 decisions-log entry "Diagnostic probe (PR
        #220) CONFIRMS root cause" for why this exists.
        """
        # Local import to break a circular dependency at module-import time —
        # :mod:`map_file_synthesis` imports nothing from this module today,
        # but the import-time order matters when api lifespan wires both
        # via :class:`BarSyncWorker`. Following the same pattern as the
        # ``bar_sync_alerts`` import in ``__init__``.
        from services.data import map_file_synthesis as _mfs

        for market_key, meta in self._config.markets.items():
            if meta.kind != "futures":
                continue
            try:
                _mfs.synthesize_futures_map_file(
                    data_root=self._config.data_root,
                    ticker=meta.ibkr_symbol,
                    market_dir=meta.market_dir,
                    market_code=meta.lean_market_code,
                    persistence_days=self._config.map_file_persistence_days,
                )
            except Exception:
                self._log.exception(
                    "map_file_synthesis_failed",
                    market=market_key,
                    ticker=meta.ibkr_symbol,
                    market_dir=meta.market_dir,
                )

    async def _backfill_historical_contracts(
        self, ib: Any, *, today: date
    ) -> tuple[HistoricalContractBackfillResult, ...]:
        """Backfill historical-contract daily CSVs for every futures market.

        Walks the futures markets in :attr:`BarSyncConfig.markets` and
        calls :func:`backfill_historical_contracts_for_ticker` for each,
        feeding in the per-ticker historical-expiry set derived from
        on-disk universe sessions via
        :func:`historical_contract_months_from_disk` (which delegates
        to :func:`services.data.map_file_synthesis.detect_real_rolls`).

        Per-ticker exceptions are caught + logged so a single ticker's
        I/O / IBKR failure doesn't abort the backfill loop. Returns the
        per-ticker results as a tuple so test code (+ future cycle-level
        observability) can inspect.

        Why this lives inside the BarSyncWorker class rather than as a
        free function: it shares the in-process ``ib`` connection with
        ``sync_one_market`` and runs inside the connect-succeeded arm
        of :meth:`run_cycle`'s try block — same lifecycle as the
        per-market sync loop. The free-function
        :func:`backfill_historical_contracts_for_ticker` is the per-
        ticker workhorse; this method is the per-cycle orchestrator.

        See the 2026-05-23 decisions-log entry "Historical contract
        backfill unblocks MA_SLOW=200" for the saga + the architectural
        rationale (IBKR ContFuture only serves the current front-
        month's bars; the strategy's MA_SLOW_DAYS=200 floor needs the
        union across historical contracts).
        """
        results: list[HistoricalContractBackfillResult] = []
        for market_key, meta in self._config.markets.items():
            if meta.kind != "futures":
                continue
            try:
                months, expiry_dates = historical_contract_months_from_disk(
                    data_root=self._config.data_root,
                    meta=meta,
                    persistence_days=self._config.map_file_persistence_days,
                )
            except Exception:
                self._log.exception(
                    "historical_contracts_backfill_disk_scan_failed",
                    market=market_key,
                    ticker=meta.ibkr_symbol,
                    market_dir=meta.market_dir,
                )
                continue
            if not months:
                # No detected rolls yet — the synthesizer's persistence
                # filter hasn't seen enough universe history for this
                # ticker. Backfill no-op + log so the operator can see
                # the per-ticker disposition.
                self._log.info(
                    "historical_contracts_backfill_skip_no_rolls",
                    market=market_key,
                    ticker=meta.ibkr_symbol,
                    market_dir=meta.market_dir,
                )
                continue
            try:
                ticker_result = await backfill_historical_contracts_for_ticker(
                    ib,
                    market_key,
                    meta=meta,
                    contract_months_to_ensure=months,
                    expiry_dates=expiry_dates,
                    data_root=self._config.data_root,
                    bars_per_contract=self._config.historical_backfill_bars_per_contract,
                    call_timeout_seconds=self._config.ibkr_call_timeout_seconds,
                )
            except Exception:
                self._log.exception(
                    "historical_contracts_backfill_for_ticker_failed",
                    market=market_key,
                    ticker=meta.ibkr_symbol,
                    market_dir=meta.market_dir,
                )
                continue
            results.append(ticker_result)
        self._log.info(
            "historical_contracts_backfill_cycle_completed",
            session_date_et=today.isoformat(),
            tickers_processed=len(results),
            tickers_with_new_contracts=sum(1 for r in results if r.backfilled_expiries),
            new_contracts_total=sum(len(r.backfilled_expiries) for r in results),
            skipped_contracts_total=sum(len(r.skipped_expiries) for r in results),
            failed_contracts_total=sum(len(r.failed_expiries) for r in results),
            bars_fetched_total=sum(r.bars_fetched_total for r in results),
        )
        return tuple(results)

    async def _evaluate_alerts(self, cycle_result: BarSyncCycleResult) -> None:
        """Update consecutive-cycle counters + dispatch P2 alerts at threshold.

        Two alert flavors evaluated independently:

        * **Partial-cycle-failure** — any market failed to sync. Counter
          increments on dirty cycles, resets on clean. Alert fires when
          ``count >= CONSECUTIVE_ALERT_THRESHOLD`` (2). Category:
          ``data_quality_reject``.
        * **OI-sentinel-substitution** — any successful market had OI
          sentinel-substituted (Task 2 path). Counter increments on
          sentinel cycles, resets when no sentinels fire. Alert fires
          when ``count >= 2``. Category: ``data_quality_quarantine``.
          Surfaces /MCL's persistent entitlement gap.

        When a ``partial_cycle_alert_hook`` is wired (separate api-
        lifespan PR), each descriptor is dispatched via the hook. When
        unwired (Phase 1 day-1 default), the descriptor is logged via
        ``bar_sync_alert_dropped_no_hook`` and dropped — the structured
        log carries the descriptor fields so the operator can grep for
        the pattern manually.

        Hook errors are caught + logged as
        ``bar_sync_alert_dispatch_failed`` so a Discord outage doesn't
        wedge the cycle.
        """
        had_failure = self._bsa.cycle_had_failures(cycle_result)
        had_sentinel = self._bsa.cycle_had_sentinel_substitution(cycle_result)
        if had_failure:
            self._consecutive_failure_count += 1
        else:
            self._consecutive_failure_count = 0
        if had_sentinel:
            self._consecutive_sentinel_count += 1
        else:
            self._consecutive_sentinel_count = 0
        partial_descriptor = self._bsa.build_partial_cycle_alert(
            cycle_result,
            consecutive_failure_count=self._consecutive_failure_count,
        )
        sentinel_descriptor = self._bsa.build_sentinel_substitution_alert(
            cycle_result,
            consecutive_sentinel_count=self._consecutive_sentinel_count,
        )
        for descriptor in (partial_descriptor, sentinel_descriptor):
            if descriptor is None:
                continue
            await self._dispatch_alert(descriptor)

    async def _dispatch_alert(self, descriptor: Any) -> None:
        """Invoke the hook (if wired) or log the descriptor + drop.

        Hook exceptions are caught + logged so an alert-side failure
        doesn't wedge the cycle. The Phase-1-day-1 default (no hook)
        logs the descriptor fields under ``bar_sync_alert_dropped_no_hook``
        so the operator can grep for the pattern manually.
        """
        if self._partial_cycle_alert_hook is None:
            self._log.warning(
                "bar_sync_alert_dropped_no_hook",
                severity=descriptor.severity,
                category=descriptor.category,
                title=descriptor.title,
                payload=descriptor.payload,
            )
            return
        try:
            await self._partial_cycle_alert_hook(descriptor)
        except Exception:
            self._log.exception(
                "bar_sync_alert_dispatch_failed",
                severity=descriptor.severity,
                category=descriptor.category,
                title=descriptor.title,
            )

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
    "DEFAULT_HISTORICAL_BACKFILL_BARS",
    "DEFAULT_IBKR_CALL_TIMEOUT_SECONDS",
    "DEFAULT_OI_MARKET_DATA_TYPE",
    "DEFAULT_OI_WAIT_SECONDS",
    "DEFAULT_SYNC_TIME_ET",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "PHASE1_UNIVERSE_METADATA",
    "SENTINEL_OI_WHEN_FETCH_FAILED",
    "Bar",
    "BarSyncConfig",
    "BarSyncCycleResult",
    "BarSyncWorker",
    "HistoricalContractBackfillResult",
    "HistoricalDataFetcher",
    "IbFactory",
    "MarketMeta",
    "MarketSyncResult",
    "backfill_historical_contracts_for_ticker",
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
    "fetch_historical_contract_bars",
    "futures_map_file_path",
    "futures_oi_zip_path",
    "futures_trade_zip_path",
    "futures_universe_file_path",
    "historical_contract_months_from_disk",
    "list_zip_member_names",
    "parse_ibkr_bars",
    "pick_front_month_expiry",
    "should_fire_now",
    "sync_one_market",
    "write_etf_bundle",
    "write_futures_bundle",
    "write_zip_with_member",
    "write_zip_with_members_preserving",
]
