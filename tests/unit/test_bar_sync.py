"""Unit tests for ``services.data.bar_sync`` — bar synchronization from
IBKR → LEAN on-disk format (Option C of the 2026-05-20 data-layer pivot v2).

Coverage:

* TestModuleConstants — locked universe + locked constants
* TestShouldFireNow / TestCurrentSessionDateEt — pure-policy schedule helpers
* TestFormatFuturesPrice — futures CSV price formatting (integer / fractional)
* TestBuildEquityDailyCsv / TestBuildEquityMapFile / TestBuildEquityFactorFile
* TestBuildFuturesTradeCsv / TestBuildFuturesOiCsv / TestBuildFuturesUniverseCsv
* TestBuildFuturesMapFile
* TestPathComputations — every path helper
* TestWriteZipWithMember / TestWriteEtfBundle / TestWriteFuturesBundle —
  filesystem writers via ``tmp_path``
* TestCoerceDecimal / TestBarDataToBar / TestParseIbkrBars — ib-async
  boundary translation
* TestPickFrontMonthExpiry — front-month resolution from ``ContractDetails``
* TestFetchEtfBars / TestFetchFuturesBarsAndFrontMonth — fetcher seams
  via fake IB
* TestSyncOneMarket — per-market orchestrator + error packaging
* TestBarSyncWorkerScheduling / TestBarSyncWorkerCycle — long-lived worker
* TestModuleContract — public ``__all__`` surface

Pure-Python tests; no testcontainers; no real IBKR I/O. A22 N/A. The
ib-async boundary is exercised via fake ``IB`` objects + fake
``BarData`` / ``ContractDetails`` shapes (the real library is
installed but the tests never invoke its network paths).
"""

from __future__ import annotations

import asyncio
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from services.data import bar_sync
from services.data.bar_sync import (
    DEFAULT_BAR_SYNC_CLIENT_ID,
    DEFAULT_BARS_PER_FETCH,
    DEFAULT_DATA_ROOT,
    DEFAULT_OI_WAIT_SECONDS,
    DEFAULT_SYNC_TIME_ET,
    DEFAULT_TICK_INTERVAL_SECONDS,
    PHASE1_UNIVERSE_METADATA,
    SENTINEL_OI_WHEN_FETCH_FAILED,
    Bar,
    BarSyncConfig,
    BarSyncWorker,
    MarketMeta,
    MarketSyncResult,
    build_equity_daily_csv,
    build_equity_factor_file,
    build_equity_map_file,
    build_futures_map_file,
    build_futures_oi_csv,
    build_futures_trade_csv,
    build_futures_universe_csv,
    current_session_date_et,
    equity_daily_zip_path,
    equity_factor_file_path,
    equity_map_file_path,
    fetch_etf_bars,
    fetch_front_month_open_interest,
    fetch_futures_bars_and_front_month,
    futures_map_file_path,
    futures_oi_zip_path,
    futures_trade_zip_path,
    futures_universe_file_path,
    parse_ibkr_bars,
    pick_front_month_expiry,
    should_fire_now,
    sync_one_market,
    write_etf_bundle,
    write_futures_bundle,
    write_zip_with_member,
)

# ---------------------------------------------------------------------------
# Fixtures + small helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeBarData:
    """Duck-typed ib-async ``BarData`` for fetch tests."""

    date: Any
    open: Any
    high: Any
    low: Any
    close: Any
    volume: Any


@dataclass
class _FakeContract:
    lastTradeDateOrContractMonth: str = ""
    localSymbol: str = ""
    symbol: str = ""
    exchange: str = ""


@dataclass
class _FakeContractDetails:
    contract: _FakeContract


@dataclass
class _FakeTicker:
    """Duck-typed ib-async ``Ticker`` with mutable ``futuresOpenInterest``.

    The real ib-async ``Ticker`` populates its fields asynchronously as
    tick callbacks arrive from IBKR. Tests pre-populate
    ``futuresOpenInterest`` to whatever value (or NaN) they want the
    poll loop to observe; a None value also makes the helper treat it
    as "not yet arrived".
    """

    futuresOpenInterest: Any = None  # mirrors ib-async's camelCase


class _FakeIb:
    """Fake ib-async ``IB`` instance.

    Tests configure the canned responses by setting
    ``self.historical_bars``, ``self.contract_details``,
    ``self.connect_should_raise``, ``self.oi_value_to_serve``,
    ``self.oi_reqmktdata_should_raise``, and
    ``self.oi_cancel_should_raise``. The instance records call args so
    tests can assert on the request shape.
    """

    def __init__(self) -> None:
        self.historical_bars: list[Any] = []
        self.contract_details: list[Any] = []
        self.connect_should_raise: Exception | None = None
        self.req_historical_calls: list[dict[str, Any]] = []
        self.req_contract_details_calls: list[Any] = []
        self.disconnect_calls = 0
        self._connected = False
        # OI snapshot canned response. ``None`` = "ticker stays empty"
        # (no tick arrived); a number = "ticker's futuresOpenInterest
        # is set to that value at subscribe time" (i.e., the very next
        # poll sees it).
        self.oi_value_to_serve: Any = None
        self.oi_reqmktdata_should_raise: Exception | None = None
        self.oi_cancel_should_raise: Exception | None = None
        self.req_mkt_data_calls: list[dict[str, Any]] = []
        self.cancel_mkt_data_calls: list[Any] = []
        # qualifyContractsAsync canned response. Default = "qualify
        # succeeds and returns the contract unchanged". Tests can
        # override to simulate failure / no-match / raise.
        self.qualify_should_raise: Exception | None = None
        self.qualify_returns_empty: bool = False
        self.qualify_returns_none_slot: bool = False
        self.qualify_calls: list[Any] = []
        # reqMarketDataType canned behavior. By default succeeds silently;
        # tests can toggle to verify the call and assert on raise paths.
        self.req_market_data_type_calls: list[int] = []
        self.req_market_data_type_should_raise: Exception | None = None

    async def connectAsync(
        self,
        *,
        host: str,
        port: int,
        clientId: int,
        timeout: float,  # noqa: ASYNC109 — mirrors ib-async's real signature
    ) -> None:
        if self.connect_should_raise is not None:
            raise self.connect_should_raise
        self._connected = True

    async def reqHistoricalDataAsync(
        self,
        contract: Any,
        *,
        endDateTime: str,
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: bool,
        formatDate: int,
    ) -> list[Any]:
        self.req_historical_calls.append(
            {
                "contract": contract,
                "endDateTime": endDateTime,
                "durationStr": durationStr,
                "barSizeSetting": barSizeSetting,
                "whatToShow": whatToShow,
                "useRTH": useRTH,
                "formatDate": formatDate,
            }
        )
        return list(self.historical_bars)

    async def reqContractDetailsAsync(self, contract: Any) -> list[Any]:
        self.req_contract_details_calls.append(contract)
        return list(self.contract_details)

    def reqMktData(
        self,
        contract: Any,
        genericTickList: str = "",
        snapshot: bool = False,
        regulatorySnapshot: bool = False,
        mktDataOptions: Any = None,
    ) -> _FakeTicker:
        self.req_mkt_data_calls.append(
            {
                "contract": contract,
                "genericTickList": genericTickList,
                "snapshot": snapshot,
            }
        )
        if self.oi_reqmktdata_should_raise is not None:
            raise self.oi_reqmktdata_should_raise
        return _FakeTicker(futuresOpenInterest=self.oi_value_to_serve)

    def cancelMktData(self, contract: Any) -> None:
        self.cancel_mkt_data_calls.append(contract)
        if self.oi_cancel_should_raise is not None:
            raise self.oi_cancel_should_raise

    def reqMarketDataType(self, marketDataType: int) -> None:
        self.req_market_data_type_calls.append(marketDataType)
        if self.req_market_data_type_should_raise is not None:
            raise self.req_market_data_type_should_raise

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:
        self.qualify_calls.append(list(contracts))
        if self.qualify_should_raise is not None:
            raise self.qualify_should_raise
        if self.qualify_returns_empty:
            return []
        if self.qualify_returns_none_slot:
            return [None for _ in contracts]
        # Real ib-async populates conId in-place; mimic the mutation so
        # downstream code paths can observe the qualified state.
        for c in contracts:
            try:
                c.conId = 999  # deterministic test sentinel
            except (AttributeError, TypeError):  # pragma: no cover — frozen contract
                pass
        return list(contracts)

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False


def _make_bar(
    *,
    session_date: date | None = None,
    open: str = "100.0",
    high: str = "101.0",
    low: str = "99.0",
    close: str = "100.5",
    volume: int = 1000,
) -> Bar:
    """Construct a Bar with sensible defaults; tests override fields."""
    return Bar(
        session_date=session_date or date(2026, 5, 19),
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


# ---------------------------------------------------------------------------
# Module constants + universe
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_sync_time_et_is_17_00(self) -> None:
        assert DEFAULT_SYNC_TIME_ET == time(hour=17, minute=0)

    def test_default_tick_interval_60s(self) -> None:
        assert DEFAULT_TICK_INTERVAL_SECONDS == 60.0

    def test_default_bars_per_fetch_250(self) -> None:
        assert DEFAULT_BARS_PER_FETCH == 250

    def test_default_data_root_is_lean_data(self) -> None:
        assert DEFAULT_DATA_ROOT == Path("/Lean/Data")

    def test_default_client_id_is_3(self) -> None:
        # Synced to deploy reality 2026-05-21: api worker=1 (deploy
        # override to 2), bar_sync=3, probes=80-99, reserve 4-7. See
        # dev-guide §1.5 LOCKED + decisions-log 2026-05-21 follow-up.
        assert DEFAULT_BAR_SYNC_CLIENT_ID == 3

    def test_default_oi_wait_seconds_5s(self) -> None:
        # Locked at 5s - IBKR's futures generic-tick 588 typically arrives
        # within 1-2s; 5s gives comfortable headroom without lengthening
        # the cycle materially.
        assert DEFAULT_OI_WAIT_SECONDS == 5.0

    def test_default_oi_market_data_type_is_delayed(self) -> None:
        # Locked at 3 (DELAYED) - paper account has no real-time
        # futures-OI entitlement; delayed is free for any IBKR account
        # holder. Override to 1 (LIVE) if/when a real-time subscription
        # is acquired.
        from services.data.bar_sync import DEFAULT_OI_MARKET_DATA_TYPE

        assert DEFAULT_OI_MARKET_DATA_TYPE == 3

    def test_default_sentinel_is_1(self) -> None:
        # Operator-approved option (c) follow-up to the 2026-05-21 saga:
        # /MCL's NYMEX delayed feed publishes OI=0 (not NaN), so the
        # fetch returns 0. Substituting a sentinel of 1 lets LEAN's
        # DataMappingMode.OPEN_INTEREST resolver pick the contract.
        assert SENTINEL_OI_WHEN_FETCH_FAILED == 1


class TestPhase1UniverseMetadata:
    def test_universe_has_11_entries(self) -> None:
        assert len(PHASE1_UNIVERSE_METADATA) == 11

    def test_universe_includes_4_etfs_and_7_futures(self) -> None:
        etfs = [k for k, v in PHASE1_UNIVERSE_METADATA.items() if v.kind == "etf"]
        futures = [k for k, v in PHASE1_UNIVERSE_METADATA.items() if v.kind == "futures"]
        assert len(etfs) == 4
        assert len(futures) == 7

    def test_etf_keys_are_bare_tickers(self) -> None:
        etfs = sorted(k for k, v in PHASE1_UNIVERSE_METADATA.items() if v.kind == "etf")
        assert etfs == ["IEF", "SHY", "TIP", "TLT"]

    def test_futures_keys_are_slash_prefixed(self) -> None:
        futs = sorted(k for k, v in PHASE1_UNIVERSE_METADATA.items() if v.kind == "futures")
        assert futs == ["/M2K", "/MBT", "/MCL", "/MES", "/MGC", "/MNQ", "/MYM"]

    def test_etf_metadata_routes_via_smart_p(self) -> None:
        for key in ("TLT", "IEF", "SHY", "TIP"):
            meta = PHASE1_UNIVERSE_METADATA[key]
            assert meta.ibkr_exchange == "SMART"
            assert meta.market_dir == "usa"
            assert meta.lean_market_code == "P"  # NYSE Arca

    def test_futures_market_dirs_partition_by_exchange(self) -> None:
        # CME family
        for key in ("/MES", "/MNQ", "/MYM", "/M2K", "/MBT"):
            meta = PHASE1_UNIVERSE_METADATA[key]
            assert meta.market_dir == "cme"
            assert meta.lean_market_code == "CME"
        # COMEX
        assert PHASE1_UNIVERSE_METADATA["/MGC"].market_dir == "comex"
        assert PHASE1_UNIVERSE_METADATA["/MGC"].lean_market_code == "COMEX"
        # NYMEX
        assert PHASE1_UNIVERSE_METADATA["/MCL"].market_dir == "nymex"
        assert PHASE1_UNIVERSE_METADATA["/MCL"].lean_market_code == "NYMEX"


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------


class TestShouldFireNow:
    def test_naive_now_utc_raises(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            should_fire_now(
                now_utc=datetime(2026, 5, 19, 21, 0),
                sync_time_et=time(17, 0),
                last_fired_session_date_et=None,
            )

    def test_before_sync_time_returns_false(self) -> None:
        # 20:00 UTC = 16:00 ET (EDT); sync_time is 17:00 ET → too early.
        now = datetime(2026, 5, 19, 20, 0, tzinfo=UTC)
        assert (
            should_fire_now(now_utc=now, sync_time_et=time(17, 0), last_fired_session_date_et=None)
            is False
        )

    def test_at_sync_time_returns_true(self) -> None:
        # 21:00 UTC = 17:00 ET (EDT)
        now = datetime(2026, 5, 19, 21, 0, tzinfo=UTC)
        assert (
            should_fire_now(now_utc=now, sync_time_et=time(17, 0), last_fired_session_date_et=None)
            is True
        )

    def test_after_sync_time_returns_true(self) -> None:
        now = datetime(2026, 5, 19, 22, 30, tzinfo=UTC)  # 18:30 ET
        assert (
            should_fire_now(now_utc=now, sync_time_et=time(17, 0), last_fired_session_date_et=None)
            is True
        )

    def test_already_fired_today_returns_false(self) -> None:
        now = datetime(2026, 5, 19, 22, 0, tzinfo=UTC)  # 18:00 ET → after sync time
        # ET date today is 2026-05-19; same date as last_fired → no refire.
        assert (
            should_fire_now(
                now_utc=now,
                sync_time_et=time(17, 0),
                last_fired_session_date_et=date(2026, 5, 19),
            )
            is False
        )

    def test_fired_yesterday_returns_true_next_day(self) -> None:
        now = datetime(2026, 5, 20, 22, 0, tzinfo=UTC)  # 18:00 ET on 5/20
        assert (
            should_fire_now(
                now_utc=now,
                sync_time_et=time(17, 0),
                last_fired_session_date_et=date(2026, 5, 19),
            )
            is True
        )


class TestCurrentSessionDateEt:
    def test_naive_now_raises(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            current_session_date_et(datetime(2026, 5, 19, 20, 0))

    def test_utc_evening_maps_to_same_et_day(self) -> None:
        # 22:00 UTC on 5/19 = 18:00 ET on 5/19 (EDT)
        now = datetime(2026, 5, 19, 22, 0, tzinfo=UTC)
        assert current_session_date_et(now) == date(2026, 5, 19)

    def test_utc_early_morning_rolls_back_to_prior_et_day(self) -> None:
        # 03:00 UTC on 5/20 = 23:00 ET on 5/19 (EDT)
        now = datetime(2026, 5, 20, 3, 0, tzinfo=UTC)
        assert current_session_date_et(now) == date(2026, 5, 19)


# ---------------------------------------------------------------------------
# Format builders
# ---------------------------------------------------------------------------


class TestFormatFuturesPrice:
    def test_integer_prints_bare(self) -> None:
        assert bar_sync._format_futures_price(Decimal("5810")) == "5810"

    def test_quarter_tick_keeps_2_decimals(self) -> None:
        assert bar_sync._format_futures_price(Decimal("7378.25")) == "7378.25"

    def test_two_decimals_etf_style(self) -> None:
        assert bar_sync._format_futures_price(Decimal("104.15")) == "104.15"

    def test_trailing_zeros_stripped(self) -> None:
        assert bar_sync._format_futures_price(Decimal("100.1000")) == "100.1"

    def test_negative_price_formatted(self) -> None:
        # IBKR shouldn't return negative prices for active contracts but
        # the formatter shouldn't blow up if one slips through.
        assert bar_sync._format_futures_price(Decimal("-12.5")) == "-12.5"


class TestBuildEquityDailyCsv:
    def test_empty_input(self) -> None:
        result = build_equity_daily_csv([])
        # Empty bars → just a trailing newline (the join + newline).
        assert result == b"\n"

    def test_single_bar_scales_to_decicents(self) -> None:
        bar = _make_bar(
            session_date=date(2026, 5, 19),
            open="85.56",
            high="85.99",
            low="85.10",
            close="85.50",
            volume=1234567,
        )
        result = build_equity_daily_csv([bar])
        assert result == b"20260519 00:00,855600,859900,851000,855000,1234567\n"

    def test_multiple_bars_join_with_newlines(self) -> None:
        bars = [
            _make_bar(session_date=date(2026, 5, 17), close="100.00"),
            _make_bar(session_date=date(2026, 5, 19), close="101.50"),
        ]
        result = build_equity_daily_csv(bars).decode()
        assert "20260517 00:00" in result
        assert "20260519 00:00" in result
        # Trailing newline + 2 bars = 2 newlines.
        assert result.count("\n") == 2

    def test_volume_emitted_as_raw_integer(self) -> None:
        bar = _make_bar(volume=42)
        out = build_equity_daily_csv([bar]).decode()
        assert out.rstrip("\n").endswith(",42")


class TestBuildEquityMapFile:
    def test_lowercase_ticker_in_both_rows(self) -> None:
        content = build_equity_map_file("TLT", "P")
        assert content == "19980102,tlt,P\n20501231,tlt,P\n"

    def test_uppercase_normalized(self) -> None:
        content = build_equity_map_file("ief", "P")
        assert content == "19980102,ief,P\n20501231,ief,P\n"


class TestBuildEquityFactorFile:
    def test_ref_price_formatted_4_decimals(self) -> None:
        content = build_equity_factor_file(Decimal("83.0200"))
        assert content == "19980102,1,1,83.0200\n20501231,1,1,0\n"

    def test_integer_ref_price_padded_to_4_decimals(self) -> None:
        content = build_equity_factor_file(Decimal("100"))
        assert content == "19980102,1,1,100.0000\n20501231,1,1,0\n"


class TestBuildFuturesTradeCsv:
    def test_empty_input(self) -> None:
        assert build_futures_trade_csv([]) == b"\n"

    def test_single_quarter_tick_bar(self) -> None:
        bar = _make_bar(
            session_date=date(2026, 5, 19),
            open="7378.00",
            high="7380.50",
            low="7375.25",
            close="7378.25",
            volume=12500,
        )
        result = build_futures_trade_csv([bar]).decode()
        # Note: raw float prices, not deci-cent scaled.
        assert result == "20260519 00:00,7378,7380.5,7375.25,7378.25,12500\n"

    def test_multiple_bars_join(self) -> None:
        bars = [
            _make_bar(session_date=date(2026, 5, 17), close="5810"),
            _make_bar(session_date=date(2026, 5, 19), close="5815"),
        ]
        result = build_futures_trade_csv(bars).decode()
        assert result.count("\n") == 2


class TestBuildFuturesOiCsv:
    def test_empty_bars(self) -> None:
        # build_futures_oi_csv iterates bars; empty bars → just the trailing newline.
        assert build_futures_oi_csv([], oi=12345) == b"\n"

    def test_single_bar(self) -> None:
        bar = _make_bar(session_date=date(2026, 5, 19))
        result = build_futures_oi_csv([bar], oi=98765).decode()
        assert result == "20260519 00:00,98765\n"

    def test_multiple_bars_share_oi(self) -> None:
        bars = [
            _make_bar(session_date=date(2026, 5, 17)),
            _make_bar(session_date=date(2026, 5, 19)),
        ]
        result = build_futures_oi_csv(bars, oi=1000).decode()
        assert result == "20260517 00:00,1000\n20260519 00:00,1000\n"


class TestBuildFuturesUniverseCsv:
    def test_includes_header_and_row(self) -> None:
        bar = _make_bar(
            session_date=date(2026, 5, 19),
            open="7378.00",
            high="7380",
            low="7375.5",
            close="7378.25",
            volume=10000,
        )
        result = build_futures_universe_csv("202606", bar, oi=500).decode()
        assert result.startswith("#expiry,open,high,low,close,volume,open_interest\n")
        assert "202606,7378,7380,7375.5,7378.25,10000,500" in result

    def test_oi_none_renders_empty_string(self) -> None:
        bar = _make_bar(session_date=date(2026, 5, 19), close="5000")
        result = build_futures_universe_csv("202606", bar, oi=None).decode()
        # Trailing empty field after the volume comma.
        assert result.rstrip("\n").endswith(",")


class TestBuildFuturesMapFile:
    def test_two_row_sentinel_format(self) -> None:
        content = build_futures_map_file("MES", "CME")
        assert content == "18991230,mes\n20501231,mes,CME\n"

    def test_market_code_uppercase_passed_through(self) -> None:
        content = build_futures_map_file("MGC", "COMEX")
        assert "COMEX" in content


# ---------------------------------------------------------------------------
# Path computations
# ---------------------------------------------------------------------------


class TestPathComputations:
    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        return tmp_path

    def test_equity_daily_zip_path(self, root: Path) -> None:
        p = equity_daily_zip_path(root, "TLT")
        assert p == root / "equity" / "usa" / "daily" / "tlt.zip"

    def test_equity_map_file_path(self, root: Path) -> None:
        p = equity_map_file_path(root, "TLT")
        assert p == root / "equity" / "usa" / "map_files" / "tlt.csv"

    def test_equity_factor_file_path(self, root: Path) -> None:
        p = equity_factor_file_path(root, "TLT")
        assert p == root / "equity" / "usa" / "factor_files" / "tlt.csv"

    def test_futures_trade_zip_path(self, root: Path) -> None:
        p = futures_trade_zip_path(root, "MES", "cme")
        assert p == root / "future" / "cme" / "daily" / "mes_trade.zip"

    def test_futures_oi_zip_path(self, root: Path) -> None:
        p = futures_oi_zip_path(root, "MGC", "comex")
        assert p == root / "future" / "comex" / "daily" / "mgc_openinterest.zip"

    def test_futures_universe_file_path(self, root: Path) -> None:
        p = futures_universe_file_path(root, "MES", "cme", date(2026, 5, 19))
        assert p == root / "future" / "cme" / "universes" / "mes" / "20260519.csv"

    def test_futures_map_file_path(self, root: Path) -> None:
        p = futures_map_file_path(root, "MCL", "nymex")
        assert p == root / "future" / "nymex" / "map_files" / "mcl.csv"

    def test_lowercase_normalization_on_uppercase_tickers(self, root: Path) -> None:
        # All path helpers must lowercase the ticker portion of the path
        # since the LEAN on-disk convention is lowercase + the strategy
        # uses lowercase paths in its history-reader resolver.
        assert "/mes_trade.zip" in str(futures_trade_zip_path(root, "mes", "cme"))
        assert "/MES" not in str(futures_trade_zip_path(root, "MES", "cme")).replace("/MES_", "")


# ---------------------------------------------------------------------------
# Filesystem writers
# ---------------------------------------------------------------------------


class TestWriteZipWithMember:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "deep" / "nested" / "out.zip"
        size = write_zip_with_member(zip_path, "hello.txt", b"world\n")
        assert zip_path.exists()
        assert size > 0
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.read("hello.txt") == b"world\n"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "out.zip"
        write_zip_with_member(zip_path, "member.csv", b"old\n")
        write_zip_with_member(zip_path, "member.csv", b"new\n")
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.read("member.csv") == b"new\n"


class TestWriteEtfBundle:
    def test_happy_path_writes_3_files(self, tmp_path: Path) -> None:
        bars = [
            _make_bar(session_date=date(2026, 5, 17), close="100.00"),
            _make_bar(session_date=date(2026, 5, 19), close="101.50"),
        ]
        size = write_etf_bundle(
            data_root=tmp_path,
            ticker="TLT",
            exchange="P",
            bars=bars,
        )
        assert size > 0
        assert equity_daily_zip_path(tmp_path, "TLT").exists()
        assert equity_map_file_path(tmp_path, "TLT").exists()
        assert equity_factor_file_path(tmp_path, "TLT").exists()

    def test_empty_bars_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="bars is empty"):
            write_etf_bundle(data_root=tmp_path, ticker="TLT", exchange="P", bars=[])

    def test_zip_contains_lower_csv_with_data(self, tmp_path: Path) -> None:
        bars = [_make_bar(close="83.02")]
        write_etf_bundle(data_root=tmp_path, ticker="TLT", exchange="P", bars=bars)
        with zipfile.ZipFile(equity_daily_zip_path(tmp_path, "TLT")) as zf:
            content = zf.read("tlt.csv").decode()
        assert "20260519 00:00" in content
        assert "830200" in content  # 83.02 * 10000

    def test_factor_file_uses_last_close(self, tmp_path: Path) -> None:
        bars = [
            _make_bar(session_date=date(2026, 5, 17), close="83.50"),
            _make_bar(session_date=date(2026, 5, 19), close="83.02"),
        ]
        write_etf_bundle(data_root=tmp_path, ticker="TLT", exchange="P", bars=bars)
        factor_content = equity_factor_file_path(tmp_path, "TLT").read_text()
        # Last close = 83.02 → 4-decimal format = 83.0200
        assert "83.0200" in factor_content

    def test_map_file_uses_exchange_code(self, tmp_path: Path) -> None:
        bars = [_make_bar()]
        write_etf_bundle(data_root=tmp_path, ticker="TLT", exchange="N", bars=bars)
        content = equity_map_file_path(tmp_path, "TLT").read_text()
        assert "tlt,N" in content


class TestWriteFuturesBundle:
    def test_empty_bars_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="bars is empty"):
            write_futures_bundle(
                data_root=tmp_path,
                ticker="MES",
                market_dir="cme",
                market_code="CME",
                front_month_expiry_yyyymm="202606",
                bars=[],
            )

    def test_writes_trade_zip_with_per_expiry_member(self, tmp_path: Path) -> None:
        bars = [_make_bar(close="7378.25")]
        size = write_futures_bundle(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            front_month_expiry_yyyymm="202606",
            bars=bars,
        )
        assert size > 0
        trade_zip = futures_trade_zip_path(tmp_path, "MES", "cme")
        with zipfile.ZipFile(trade_zip) as zf:
            assert zf.namelist() == ["mes_trade_202606.csv"]

    def test_writes_oi_zip_with_per_expiry_member(self, tmp_path: Path) -> None:
        bars = [_make_bar()]
        write_futures_bundle(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            front_month_expiry_yyyymm="202606",
            bars=bars,
        )
        oi_zip = futures_oi_zip_path(tmp_path, "MES", "cme")
        with zipfile.ZipFile(oi_zip) as zf:
            assert zf.namelist() == ["mes_openinterest_202606.csv"]

    def test_writes_one_universe_file_per_bar(self, tmp_path: Path) -> None:
        bars = [
            _make_bar(session_date=date(2026, 5, 17)),
            _make_bar(session_date=date(2026, 5, 18)),
            _make_bar(session_date=date(2026, 5, 19)),
        ]
        write_futures_bundle(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            front_month_expiry_yyyymm="202606",
            bars=bars,
        )
        universe_dir = futures_universe_file_path(tmp_path, "MES", "cme", date(2026, 5, 19)).parent
        files = sorted(p.name for p in universe_dir.iterdir())
        assert files == ["20260517.csv", "20260518.csv", "20260519.csv"]

    def test_writes_map_file_with_two_row_sentinel(self, tmp_path: Path) -> None:
        bars = [_make_bar()]
        write_futures_bundle(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            front_month_expiry_yyyymm="202606",
            bars=bars,
        )
        content = futures_map_file_path(tmp_path, "MES", "cme").read_text()
        assert content == "18991230,mes\n20501231,mes,CME\n"

    def test_universe_file_pins_front_month_expiry(self, tmp_path: Path) -> None:
        # The universe file's data row must reference the SAME expiry the
        # trade zip is bucketed under (otherwise LEAN's resolver picks an
        # expiry that doesn't exist on-disk).
        bars = [_make_bar(session_date=date(2026, 5, 19), close="7378.25")]
        write_futures_bundle(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            front_month_expiry_yyyymm="202606",
            bars=bars,
        )
        u_path = futures_universe_file_path(tmp_path, "MES", "cme", date(2026, 5, 19))
        content = u_path.read_text()
        assert content.startswith("#expiry,")
        assert "202606," in content

    def test_oi_zero_renders_empty_universe_oi(self, tmp_path: Path) -> None:
        bars = [_make_bar(session_date=date(2026, 5, 19))]
        write_futures_bundle(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            front_month_expiry_yyyymm="202606",
            bars=bars,
            open_interest=0,
        )
        u_path = futures_universe_file_path(tmp_path, "MES", "cme", date(2026, 5, 19))
        content = u_path.read_text()
        # OI=0 → universe row's OI field is empty (per the build_futures_universe_csv
        # convention: None / 0 → empty string).
        assert content.rstrip("\n").endswith(",")


# ---------------------------------------------------------------------------
# ib-async boundary translation
# ---------------------------------------------------------------------------


class TestCoerceDecimal:
    def test_integer_coerced(self) -> None:
        assert bar_sync._coerce_decimal(100) == Decimal("100")

    def test_float_coerced_via_str(self) -> None:
        assert bar_sync._coerce_decimal(85.56) == Decimal("85.56")

    def test_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            bar_sync._coerce_decimal(float("nan"))

    def test_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            bar_sync._coerce_decimal(float("inf"))


class TestBarDataToBar:
    def test_valid_float_bar_returns_bar(self) -> None:
        raw = _FakeBarData(
            date=date(2026, 5, 19),
            open=100.0,
            high=101.0,
            low=99.5,
            close=100.5,
            volume=12345,
        )
        result = bar_sync._bar_data_to_bar(raw)
        assert result is not None
        assert result.session_date == date(2026, 5, 19)
        assert result.open == Decimal("100.0")
        assert result.close == Decimal("100.5")
        assert result.volume == 12345

    def test_datetime_date_extracted(self) -> None:
        raw = _FakeBarData(
            date=datetime(2026, 5, 19, 21, 30, tzinfo=UTC),
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=1,
        )
        result = bar_sync._bar_data_to_bar(raw)
        assert result is not None
        assert result.session_date == date(2026, 5, 19)

    def test_yyyymmdd_string_date_parsed(self) -> None:
        raw = _FakeBarData(
            date="20260519",
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
        )
        result = bar_sync._bar_data_to_bar(raw)
        assert result is not None
        assert result.session_date == date(2026, 5, 19)

    def test_invalid_string_date_returns_none(self) -> None:
        raw = _FakeBarData(
            date="not-a-date",
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
        )
        assert bar_sync._bar_data_to_bar(raw) is None

    def test_missing_close_returns_none(self) -> None:
        raw = _FakeBarData(
            date=date(2026, 5, 19),
            open=100,
            high=101,
            low=99,
            close=None,
            volume=1,
        )
        assert bar_sync._bar_data_to_bar(raw) is None

    def test_negative_close_returns_none(self) -> None:
        # IBKR's "no data this session" sentinel is close=-1.
        raw = _FakeBarData(
            date=date(2026, 5, 19),
            open=100,
            high=101,
            low=99,
            close=-1,
            volume=1,
        )
        assert bar_sync._bar_data_to_bar(raw) is None

    def test_zero_close_returns_none(self) -> None:
        raw = _FakeBarData(
            date=date(2026, 5, 19),
            open=100,
            high=101,
            low=99,
            close=0,
            volume=1,
        )
        assert bar_sync._bar_data_to_bar(raw) is None

    def test_nan_open_returns_none(self) -> None:
        raw = _FakeBarData(
            date=date(2026, 5, 19),
            open=float("nan"),
            high=101,
            low=99,
            close=100,
            volume=1,
        )
        assert bar_sync._bar_data_to_bar(raw) is None

    def test_negative_volume_coerced_to_zero(self) -> None:
        raw = _FakeBarData(
            date=date(2026, 5, 19),
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=-1,
        )
        result = bar_sync._bar_data_to_bar(raw)
        assert result is not None
        assert result.volume == 0


class TestParseIbkrBars:
    def test_empty_input(self) -> None:
        assert parse_ibkr_bars([]) == []

    def test_sorted_ascending_by_date(self) -> None:
        raws = [
            _FakeBarData(date=date(2026, 5, 19), open=2, high=2, low=2, close=2, volume=2),
            _FakeBarData(date=date(2026, 5, 17), open=1, high=1, low=1, close=1, volume=1),
            _FakeBarData(date=date(2026, 5, 18), open=3, high=3, low=3, close=3, volume=3),
        ]
        bars = parse_ibkr_bars(raws)
        assert [b.session_date for b in bars] == [
            date(2026, 5, 17),
            date(2026, 5, 18),
            date(2026, 5, 19),
        ]

    def test_dedup_keeps_last_per_date(self) -> None:
        raws = [
            _FakeBarData(date=date(2026, 5, 19), open=100, high=100, low=100, close=100, volume=1),
            _FakeBarData(date=date(2026, 5, 19), open=200, high=200, low=200, close=200, volume=2),
        ]
        bars = parse_ibkr_bars(raws)
        assert len(bars) == 1
        # The dict-insertion overwrite keeps the SECOND (last) entry.
        assert bars[0].close == Decimal("200")

    def test_mixed_valid_and_invalid_filters_invalid(self) -> None:
        raws = [
            _FakeBarData(date=date(2026, 5, 17), open=100, high=100, low=100, close=100, volume=1),
            _FakeBarData(  # invalid: NaN open
                date=date(2026, 5, 18),
                open=float("nan"),
                high=101,
                low=99,
                close=100,
                volume=1,
            ),
            _FakeBarData(date=date(2026, 5, 19), open=200, high=200, low=200, close=200, volume=2),
        ]
        bars = parse_ibkr_bars(raws)
        assert len(bars) == 2
        assert [b.session_date for b in bars] == [date(2026, 5, 17), date(2026, 5, 19)]


# ---------------------------------------------------------------------------
# Front-month resolution
# ---------------------------------------------------------------------------


class TestPickFrontMonthExpiry:
    def test_empty_returns_none(self) -> None:
        assert pick_front_month_expiry([], today=date(2026, 5, 19)) is None

    def test_single_future_returns_yyyymm(self) -> None:
        cd = _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620"))
        assert pick_front_month_expiry([cd], today=date(2026, 5, 19)) == "202606"

    def test_multiple_picks_earliest_remaining(self) -> None:
        cds = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260919")),
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20261219")),
        ]
        assert pick_front_month_expiry(cds, today=date(2026, 5, 19)) == "202606"

    def test_past_expiry_filtered_out(self) -> None:
        cds = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260420")),  # past
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),  # future
        ]
        assert pick_front_month_expiry(cds, today=date(2026, 5, 19)) == "202606"

    def test_all_past_returns_none(self) -> None:
        cds = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260420")),
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260319")),
        ]
        assert pick_front_month_expiry(cds, today=date(2026, 5, 19)) is None

    def test_yyyymm_only_form_accepted(self) -> None:
        # Some IBKR contracts return just YYYYMM (e.g., "202606") rather
        # than YYYYMMDD. Treat as last-day-of-month for the past-check.
        cd = _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="202606"))
        assert pick_front_month_expiry([cd], today=date(2026, 5, 19)) == "202606"

    def test_malformed_skipped(self) -> None:
        cds = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="")),
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        assert pick_front_month_expiry(cds, today=date(2026, 5, 19)) == "202606"

    def test_missing_contract_attribute_skipped(self) -> None:
        @dataclass
        class _NoContract:
            pass

        cd = _NoContract()
        # type: ignore[list-item] — deliberately wrong shape for resilience test
        assert pick_front_month_expiry([cd], today=date(2026, 5, 19)) is None  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Fetcher seams (via fake IB)
# ---------------------------------------------------------------------------


class TestFetchEtfBars:
    @pytest.fixture
    def meta(self) -> MarketMeta:
        return PHASE1_UNIVERSE_METADATA["TLT"]

    def test_happy_path_returns_parsed_bars(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 17), open=82.5, high=83.0, low=82.4, close=82.9, volume=1000
            ),
            _FakeBarData(
                date=date(2026, 5, 19), open=82.9, high=83.2, low=82.7, close=83.02, volume=2000
            ),
        ]
        bars = asyncio.run(
            fetch_etf_bars(
                ib,
                "TLT",
                meta=meta,
                bars_count=250,
                call_timeout_seconds=10.0,
            )
        )
        assert len(bars) == 2
        assert bars[-1].close == Decimal("83.02")

    def test_passes_duration_str_format(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.historical_bars = []
        asyncio.run(
            fetch_etf_bars(
                ib,
                "TLT",
                meta=meta,
                bars_count=180,
                call_timeout_seconds=10.0,
            )
        )
        assert len(ib.req_historical_calls) == 1
        call = ib.req_historical_calls[0]
        assert call["durationStr"] == "180 D"
        assert call["barSizeSetting"] == "1 day"
        assert call["whatToShow"] == "TRADES"
        assert call["useRTH"] is True
        assert call["formatDate"] == 2  # UTC

    def test_wrong_meta_kind_raises(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        futures_meta = PHASE1_UNIVERSE_METADATA["/MES"]
        with pytest.raises(ValueError, match="non-ETF"):
            asyncio.run(
                fetch_etf_bars(
                    ib,
                    "/MES",
                    meta=futures_meta,
                    bars_count=10,
                    call_timeout_seconds=10.0,
                )
            )


class TestFetchFuturesBarsAndFrontMonth:
    @pytest.fixture
    def meta(self) -> MarketMeta:
        return PHASE1_UNIVERSE_METADATA["/MES"]

    def test_happy_path_returns_bars_and_front_month(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260919")),
        ]
        bars, front_month = asyncio.run(
            fetch_futures_bars_and_front_month(
                ib,
                "/MES",
                meta=meta,
                bars_count=250,
                call_timeout_seconds=10.0,
                today=date(2026, 5, 19),
            )
        )
        assert len(bars) == 1
        assert front_month == "202606"

    def test_uses_useRTH_false_for_futures(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        asyncio.run(
            fetch_futures_bars_and_front_month(
                ib,
                "/MES",
                meta=meta,
                bars_count=250,
                call_timeout_seconds=10.0,
                today=date(2026, 5, 19),
            )
        )
        call = ib.req_historical_calls[0]
        assert call["useRTH"] is False  # futures trade ~23h

    def test_no_live_contracts_raises(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260320")),  # past
        ]
        with pytest.raises(ValueError, match="no live front-month"):
            asyncio.run(
                fetch_futures_bars_and_front_month(
                    ib,
                    "/MES",
                    meta=meta,
                    bars_count=250,
                    call_timeout_seconds=10.0,
                    today=date(2026, 5, 19),
                )
            )

    def test_wrong_meta_kind_raises(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        etf_meta = PHASE1_UNIVERSE_METADATA["TLT"]
        with pytest.raises(ValueError, match="non-futures"):
            asyncio.run(
                fetch_futures_bars_and_front_month(
                    ib,
                    "TLT",
                    meta=etf_meta,
                    bars_count=10,
                    call_timeout_seconds=10.0,
                    today=date(2026, 5, 19),
                )
            )


class TestFetchFrontMonthOpenInterest:
    """OI snapshot via reqMktData generic-tick 588 (futures only).

    Closes the 2026-05-20 OI bug: pre-fix, ``sync_one_market`` hardcoded
    ``open_interest=0`` in the bundle write, so universe files landed
    with empty OI columns and LEAN's ``DataMappingMode.OPEN_INTEREST``
    resolver could not pick a contract. Post-fix, this helper provides
    a real snapshot value the writer threads into the universe row.
    """

    @pytest.fixture
    def meta(self) -> MarketMeta:
        return PHASE1_UNIVERSE_METADATA["/MES"]

    def test_happy_path_returns_int_oi(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.oi_value_to_serve = 257985.0  # IBKR ticks are float
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=1.0,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 257985
        assert isinstance(oi, int)
        # Subscription must include generic tick 588 + NOT snapshot mode
        # (IBKR's snapshot=True doesn't deliver generic ticks).
        assert len(ib.req_mkt_data_calls) == 1
        call = ib.req_mkt_data_calls[0]
        assert call["genericTickList"] == "588"
        assert call["snapshot"] is False
        # Contract is the front-month Future, not a ContFuture.
        assert call["contract"].lastTradeDateOrContractMonth == "202606"

    def test_cancel_always_called_on_success(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.oi_value_to_serve = 100
        asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.5,
                poll_interval_seconds=0.01,
            )
        )
        assert len(ib.cancel_mkt_data_calls) == 1

    def test_nan_oi_returns_zero(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.oi_value_to_serve = float("nan")
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.2,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        # Cancellation still happens.
        assert len(ib.cancel_mkt_data_calls) == 1

    def test_none_oi_returns_zero_on_timeout(self, meta: MarketMeta) -> None:
        # Ticker stays unpopulated (None) — simulates IBKR never sending
        # the tick (no subscription entitlement, network issue, etc.).
        ib = _FakeIb()
        ib.oi_value_to_serve = None
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.15,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0

    def test_zero_oi_keeps_polling_until_timeout(self, meta: MarketMeta) -> None:
        # Some IBKR feeds emit OI=0 sentinel before real data arrives;
        # the helper should not return 0 prematurely — it should keep
        # polling within the wall-clock budget. We assert via the poll
        # count by checking that the helper waited the whole budget.
        ib = _FakeIb()
        ib.oi_value_to_serve = 0.0
        import time as _time

        t0 = _time.monotonic()
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.15,
                poll_interval_seconds=0.02,
            )
        )
        elapsed = _time.monotonic() - t0
        assert oi == 0
        # Should have polled near the full budget (allow generous slack
        # for scheduler jitter).
        assert elapsed >= 0.12

    def test_negative_oi_treated_as_unavailable(self, meta: MarketMeta) -> None:
        # IBKR occasionally returns -1 as an "unavailable" sentinel on
        # other tick types; the OI poll should not return -1 as a real
        # int either.
        ib = _FakeIb()
        ib.oi_value_to_serve = -1.0
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.1,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0

    def test_inf_oi_returns_zero(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.oi_value_to_serve = float("inf")
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.1,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0

    def test_wait_seconds_zero_short_circuits(self, meta: MarketMeta) -> None:
        # wait_seconds <= 0 disables the OI fetch — no IBKR call should fire.
        ib = _FakeIb()
        ib.oi_value_to_serve = 100
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        assert len(ib.req_mkt_data_calls) == 0
        assert len(ib.cancel_mkt_data_calls) == 0

    def test_wait_seconds_negative_short_circuits(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.oi_value_to_serve = 100
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=-1.0,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        assert len(ib.req_mkt_data_calls) == 0

    def test_reqmktdata_raises_returns_zero(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.oi_reqmktdata_should_raise = RuntimeError("ib-async exploded")
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.1,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        # cancelMktData is only called if reqMktData succeeded; since we
        # raised at reqMktData, cancel should never run.
        assert len(ib.cancel_mkt_data_calls) == 0

    def test_cancel_failure_swallowed(self, meta: MarketMeta) -> None:
        # A failure inside cancelMktData (e.g., contract no longer
        # subscribed) must not propagate — the caller already has the
        # OI value or knows to fall back to 0.
        ib = _FakeIb()
        ib.oi_value_to_serve = 500
        ib.oi_cancel_should_raise = RuntimeError("already cancelled")
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.5,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 500  # main value preserved
        assert len(ib.cancel_mkt_data_calls) == 1

    def test_non_futures_meta_raises(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        etf_meta = PHASE1_UNIVERSE_METADATA["TLT"]
        with pytest.raises(ValueError, match="non-futures"):
            asyncio.run(
                fetch_front_month_open_interest(
                    ib,
                    "TLT",
                    meta=etf_meta,
                    front_month_expiry_yyyymm="202606",
                    wait_seconds=0.1,
                )
            )

    def test_string_oi_value_returns_zero(self, meta: MarketMeta) -> None:
        # Defensive: if a future ib-async version changes the field type
        # to a string, our int() conversion attempt should fall back
        # gracefully rather than raise.
        ib = _FakeIb()
        ib.oi_value_to_serve = "not-a-number"
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.1,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0

    def test_qualify_called_before_reqmktdata(self, meta: MarketMeta) -> None:
        # ib-async's reqMktData calls hash(contract) internally via
        # startTicker, which requires conId populated. Production fix
        # PR #205 hot-fix: qualifyContractsAsync must run BEFORE
        # reqMktData to populate conId. This test locks the call order.
        ib = _FakeIb()
        ib.oi_value_to_serve = 100
        asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.5,
                poll_interval_seconds=0.01,
            )
        )
        assert len(ib.qualify_calls) == 1
        # The qualified contract must be the one passed to reqMktData.
        assert len(ib.req_mkt_data_calls) == 1
        # After the qualify fake mutated the contract's conId in-place,
        # the very same object is forwarded to reqMktData.
        qualified_contract = ib.qualify_calls[0][0]
        passed_to_mktdata = ib.req_mkt_data_calls[0]["contract"]
        assert qualified_contract is passed_to_mktdata
        # And conId was populated by the fake's qualify mutation.
        assert getattr(qualified_contract, "conId", 0) == 999

    def test_qualify_raises_returns_zero(self, meta: MarketMeta) -> None:
        ib = _FakeIb()
        ib.oi_value_to_serve = 100
        ib.qualify_should_raise = RuntimeError("ib-async qualify exploded")
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.5,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        # reqMktData must NOT fire when qualify fails — it would raise
        # the same ValueError that motivated this hot-fix.
        assert len(ib.req_mkt_data_calls) == 0

    def test_qualify_returns_empty_returns_zero(self, meta: MarketMeta) -> None:
        # ib-async returns ``[]`` from qualifyContractsAsync when the
        # internal reqContractDetails call returns no rows (unknown
        # symbol or bad exchange).
        ib = _FakeIb()
        ib.oi_value_to_serve = 100
        ib.qualify_returns_empty = True
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.5,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        assert len(ib.req_mkt_data_calls) == 0

    def test_qualify_returns_none_slot_returns_zero(self, meta: MarketMeta) -> None:
        # ib-async returns ``[None]`` when the contract was ambiguous
        # (multiple matches and ``returnAll=False``).
        ib = _FakeIb()
        ib.oi_value_to_serve = 100
        ib.qualify_returns_none_slot = True
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.5,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        assert len(ib.req_mkt_data_calls) == 0

    def test_qualify_unavailable_returns_zero(self, meta: MarketMeta) -> None:
        # Defensive: if the ib client doesn't have qualifyContractsAsync
        # (older ib-async or a non-conforming fake), degrade gracefully
        # rather than raising AttributeError.
        class _BareIb(_FakeIb):
            qualifyContractsAsync = None  # type: ignore[assignment]

        ib = _BareIb()
        ib.oi_value_to_serve = 100
        oi = asyncio.run(
            fetch_front_month_open_interest(
                ib,
                "/MES",
                meta=meta,
                front_month_expiry_yyyymm="202606",
                wait_seconds=0.5,
                poll_interval_seconds=0.01,
            )
        )
        assert oi == 0
        assert len(ib.req_mkt_data_calls) == 0


# ---------------------------------------------------------------------------
# Per-market orchestrator
# ---------------------------------------------------------------------------


class TestSyncOneMarket:
    @pytest.fixture
    def config(self, tmp_path: Path) -> BarSyncConfig:
        return BarSyncConfig(
            markets=dict(PHASE1_UNIVERSE_METADATA),
            data_root=tmp_path,
            bars_per_fetch=10,
            ibkr_call_timeout_seconds=5.0,
        )

    def test_etf_happy_path_writes_files_and_returns_success(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=82.5, high=83.0, low=82.4, close=83.02, volume=1000
            ),
        ]
        result = asyncio.run(
            sync_one_market(
                ib,
                "TLT",
                PHASE1_UNIVERSE_METADATA["TLT"],
                config=config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is True
        assert result.bars_written == 1
        assert result.last_session_date == date(2026, 5, 19)
        assert result.front_month_expiry is None  # ETFs have no expiry
        assert result.open_interest is None  # ETFs have no OI
        assert result.error is None
        assert equity_daily_zip_path(tmp_path, "TLT").exists()
        # ETF path must NOT call reqMktData (OI fetch is futures-only).
        assert len(ib.req_mkt_data_calls) == 0

    def test_futures_happy_path_writes_files_and_returns_success(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 257985.0
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MES",
                PHASE1_UNIVERSE_METADATA["/MES"],
                config=config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is True
        assert result.bars_written == 1
        assert result.front_month_expiry == "202606"
        assert result.open_interest == 257985
        assert futures_trade_zip_path(tmp_path, "MES", "cme").exists()

    def test_futures_oi_propagates_to_universe_file(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # The on-disk universe file's OI column is the value LEAN's
        # DataMappingMode.OPEN_INTEREST resolver consults. This test
        # locks the end-to-end wiring: ib.oi_value_to_serve → real
        # value in the per-day universe CSV.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 257985.0
        asyncio.run(
            sync_one_market(
                ib,
                "/MES",
                PHASE1_UNIVERSE_METADATA["/MES"],
                config=config,
                today=date(2026, 5, 19),
            )
        )
        u_path = futures_universe_file_path(tmp_path, "MES", "cme", date(2026, 5, 19))
        content = u_path.read_text()
        # Universe row format: <expiry>,<o>,<h>,<l>,<c>,<v>,<oi>
        # OI must be the real value, NOT empty + NOT 0.
        assert content.rstrip("\n").endswith(",257985")
        # OI zip should also carry the value across all bar dates.
        oi_zip = futures_oi_zip_path(tmp_path, "MES", "cme")
        with zipfile.ZipFile(oi_zip) as zf:
            oi_content = zf.read("mes_openinterest_202606.csv").decode()
        assert "20260519 00:00,257985" in oi_content

    def test_futures_oi_unavailable_does_not_fail_market(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # If IBKR doesn't serve OI (subscription gap, transient failure),
        # the helper returns 0. With the default sentinel
        # ``oi_sentinel_when_fetch_failed=1``, the cycle MUST still
        # succeed AND the universe file's OI column carries the
        # sentinel value so LEAN's resolver picks the contract. Without
        # the sentinel the resolver would skip the market entirely.
        # (Operator-approved option (c) follow-up to 2026-05-21 saga.)
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = None  # ticker never populates
        # Tight budget so the test doesn't pay the full 5s default.
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
        )
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MES",
                PHASE1_UNIVERSE_METADATA["/MES"],
                config=narrow_config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is True
        assert result.bars_written == 1
        # Effective OI reflects the sentinel substitution, NOT the raw 0
        # the helper returned. Locks the contract: callers read the
        # value LEAN sees on disk.
        assert result.open_interest == SENTINEL_OI_WHEN_FETCH_FAILED == 1
        u_path = futures_universe_file_path(tmp_path, "MES", "cme", date(2026, 5, 19))
        content = u_path.read_text()
        assert content.startswith("#expiry,")
        assert "202606," in content
        # Universe row's OI column carries the sentinel (NOT empty +
        # NOT 0).
        assert content.rstrip("\n").endswith(",1")

    def test_sentinel_substitutes_when_fetch_returns_zero(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # Pin the explicit sentinel value (not just the default) to lock
        # the contract: substitution lands the exact configured value
        # in both the MarketSyncResult AND the on-disk universe file.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 0.0  # /MCL NYMEX-delayed-tier shape
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
            oi_sentinel_when_fetch_failed=7,  # arbitrary non-default
        )
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MCL",
                PHASE1_UNIVERSE_METADATA["/MCL"],
                config=narrow_config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is True
        assert result.open_interest == 7
        u_path = futures_universe_file_path(tmp_path, "MCL", "nymex", date(2026, 5, 19))
        assert u_path.read_text().rstrip("\n").endswith(",7")

    def test_sentinel_disabled_when_config_zero(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # With ``oi_sentinel_when_fetch_failed=0`` the substitution is
        # disabled — the universe file gets an empty OI column,
        # preserving the pre-2026-05-21 behavior. Operator escape hatch
        # in case the sentinel ever causes downstream issues.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 0.0
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
            oi_sentinel_when_fetch_failed=0,
        )
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MCL",
                PHASE1_UNIVERSE_METADATA["/MCL"],
                config=narrow_config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is True
        # Raw 0 propagates — no substitution.
        assert result.open_interest == 0
        u_path = futures_universe_file_path(tmp_path, "MCL", "nymex", date(2026, 5, 19))
        content = u_path.read_text()
        # Empty trailing OI column (pre-2026-05-21 behavior).
        assert content.rstrip("\n").endswith(",")
        assert not content.rstrip("\n").endswith(",0")

    def test_sentinel_does_not_override_real_oi(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # When the helper returns a real positive OI, the sentinel does
        # NOT apply — locks the contract that substitution only fires
        # when fetch returns 0. Critical for /MES/MNQ/etc. which the
        # 2026-05-21 saga showed return real OI values.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 257985.0
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
            oi_sentinel_when_fetch_failed=1,  # explicit default
        )
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MES",
                PHASE1_UNIVERSE_METADATA["/MES"],
                config=narrow_config,
                today=date(2026, 5, 19),
            )
        )
        assert result.open_interest == 257985
        u_path = futures_universe_file_path(tmp_path, "MES", "cme", date(2026, 5, 19))
        assert u_path.read_text().rstrip("\n").endswith(",257985")

    def test_sentinel_substitution_logged(self, tmp_path: Path, config: BarSyncConfig) -> None:
        # The structured log line is the only signal Task 4's partial-
        # cycle alert will see for "OI was sentinel-substituted" until
        # MarketSyncResult.open_interest_was_sentinel lands. Lock the
        # log shape so the alert query in Task 4 can grep deterministically.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 0.0
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
        )
        with capture_logs() as logs:
            asyncio.run(
                sync_one_market(
                    ib,
                    "/MCL",
                    PHASE1_UNIVERSE_METADATA["/MCL"],
                    config=narrow_config,
                    today=date(2026, 5, 19),
                )
            )
        sentinel_events = [e for e in logs if e.get("event") == "oi_sentinel_substituted"]
        assert len(sentinel_events) == 1
        evt = sentinel_events[0]
        assert evt["market"] == "/MCL"
        assert evt["front_month_expiry"] == "202606"
        assert evt["reason"] == "fetch_returned_zero"
        assert evt["raw_open_interest"] == 0
        assert evt["sentinel"] == 1
        assert evt["log_level"] == "warning"

    def test_sentinel_not_logged_when_fetch_succeeds(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # No oi_sentinel_substituted event when the helper returned a
        # real OI. Locks the contract that the log is only emitted on
        # the substitution path.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 257985.0
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
        )
        with capture_logs() as logs:
            asyncio.run(
                sync_one_market(
                    ib,
                    "/MES",
                    PHASE1_UNIVERSE_METADATA["/MES"],
                    config=narrow_config,
                    today=date(2026, 5, 19),
                )
            )
        sentinel_events = [e for e in logs if e.get("event") == "oi_sentinel_substituted"]
        assert len(sentinel_events) == 0

    def test_market_sync_result_flags_sentinel_substitution(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # Locks the Task 4 contract: MarketSyncResult.open_interest_was_sentinel
        # is True iff the substitution path fired in sync_one_market.
        # bar_sync_alerts builders depend on this field; the alert
        # seam can't fire without it.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 0.0
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
        )
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MCL",
                PHASE1_UNIVERSE_METADATA["/MCL"],
                config=narrow_config,
                today=date(2026, 5, 19),
            )
        )
        assert result.open_interest_was_sentinel is True

    def test_market_sync_result_does_not_flag_when_real_oi(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 257985.0
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
        )
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MES",
                PHASE1_UNIVERSE_METADATA["/MES"],
                config=narrow_config,
                today=date(2026, 5, 19),
            )
        )
        assert result.open_interest_was_sentinel is False

    def test_market_sync_result_does_not_flag_when_substitution_disabled(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        # Substitution disabled via config; flag stays False even on
        # OI=0 fetch.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=7378.0,
                high=7380.5,
                low=7375.25,
                close=7378.25,
                volume=12500,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 0.0
        narrow_config = BarSyncConfig(
            markets=config.markets,
            data_root=tmp_path,
            bars_per_fetch=config.bars_per_fetch,
            ibkr_call_timeout_seconds=config.ibkr_call_timeout_seconds,
            oi_wait_seconds=0.1,
            oi_sentinel_when_fetch_failed=0,
        )
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MCL",
                PHASE1_UNIVERSE_METADATA["/MCL"],
                config=narrow_config,
                today=date(2026, 5, 19),
            )
        )
        assert result.open_interest_was_sentinel is False

    def test_etf_empty_bars_returns_failure(self, tmp_path: Path, config: BarSyncConfig) -> None:
        ib = _FakeIb()
        ib.historical_bars = []
        result = asyncio.run(
            sync_one_market(
                ib,
                "TLT",
                PHASE1_UNIVERSE_METADATA["TLT"],
                config=config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is False
        assert result.bars_written == 0
        assert result.error == "ibkr_returned_no_bars"

    def test_futures_empty_bars_returns_failure(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        ib = _FakeIb()
        ib.historical_bars = []
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MES",
                PHASE1_UNIVERSE_METADATA["/MES"],
                config=config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is False
        assert result.error == "ibkr_returned_no_bars"

    def test_no_live_front_month_returns_failure(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        ib = _FakeIb()
        ib.contract_details = []  # no live contracts at all
        result = asyncio.run(
            sync_one_market(
                ib,
                "/MES",
                PHASE1_UNIVERSE_METADATA["/MES"],
                config=config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is False
        assert result.error is not None
        assert "no live front-month" in result.error

    def test_unexpected_exception_packaged_into_result(
        self, tmp_path: Path, config: BarSyncConfig
    ) -> None:
        class _Boom(_FakeIb):
            async def reqHistoricalDataAsync(self, *a: Any, **kw: Any) -> Any:
                raise RuntimeError("ib_async exploded")

        result = asyncio.run(
            sync_one_market(
                _Boom(),
                "TLT",
                PHASE1_UNIVERSE_METADATA["TLT"],
                config=config,
                today=date(2026, 5, 19),
            )
        )
        assert result.success is False
        assert "RuntimeError" in (result.error or "")


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------


class TestBarSyncWorkerCycle:
    @pytest.fixture
    def small_config(self, tmp_path: Path) -> BarSyncConfig:
        # Two-market universe — one ETF + one futures, keeps the test fast.
        # Tight oi_wait_seconds so tests don't pay the 5s production budget.
        return BarSyncConfig(
            markets={
                "TLT": PHASE1_UNIVERSE_METADATA["TLT"],
                "/MES": PHASE1_UNIVERSE_METADATA["/MES"],
            },
            data_root=tmp_path,
            bars_per_fetch=5,
            tick_interval_seconds=0.01,
            ibkr_call_timeout_seconds=2.0,
            ibkr_connect_timeout_seconds=2.0,
            oi_wait_seconds=0.1,
        )

    def test_run_cycle_happy_path_returns_two_successes(self, small_config: BarSyncConfig) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=10
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 50000
        clock_ts = datetime(2026, 5, 19, 21, 0, tzinfo=UTC)  # 17:00 ET
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: clock_ts,
        )
        result = asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert len(result.successful_markets) == 2
        assert len(result.failed_markets) == 0
        assert result.total_markets == 2
        assert ib.disconnect_calls == 1
        # OI should land on the futures market's result.
        futures_result = next(r for r in result.successful_markets if r.market == "/MES")
        assert futures_result.open_interest == 50000

    def test_run_cycle_connect_failure_marks_all_failed(self, small_config: BarSyncConfig) -> None:
        ib = _FakeIb()
        ib.connect_should_raise = ConnectionRefusedError("ib_gateway down")
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        result = asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert result.all_failed is True
        assert len(result.failed_markets) == 2
        for r in result.failed_markets:
            assert "ibkr_connect_failed" in (r.error or "")

    def test_run_cycle_per_market_mixed_outcome(self, small_config: BarSyncConfig) -> None:
        # ETF returns bars; futures has no live contracts → ETF success +
        # futures failure.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=10
            ),
        ]
        ib.contract_details = []  # no live front-month → /MES fails
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        result = asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert len(result.successful_markets) == 1
        assert len(result.failed_markets) == 1
        assert result.successful_markets[0].market == "TLT"
        assert result.failed_markets[0].market == "/MES"

    def test_run_cycle_sets_market_data_type_before_market_loop(
        self, small_config: BarSyncConfig
    ) -> None:
        # Hot-fix PR #207: the paper account has no real-time futures-OI
        # entitlement. Without reqMarketDataType(3) before reqMktData,
        # IBKR replies with Error 354 + the OI tick never arrives → 5s
        # oi_fetch_timeout per market. This test locks the call order:
        # connectAsync → reqMarketDataType → market loop.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=10
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = 50000
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        # Must be called exactly once per cycle, before the market loop.
        assert ib.req_market_data_type_calls == [3]

    def test_run_cycle_market_data_type_failure_logged_but_cycle_continues(
        self, small_config: BarSyncConfig
    ) -> None:
        # If reqMarketDataType raises, the OI fetch may fail but the
        # cycle must still write the bundle. Graceful degradation.
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=10
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.req_market_data_type_should_raise = RuntimeError("ib_async exploded")
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        result = asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        # Cycle still succeeds for both markets despite the MDT failure.
        assert len(result.successful_markets) == 2
        assert len(result.failed_markets) == 0

    def test_run_cycle_skips_market_data_type_when_absent(
        self, small_config: BarSyncConfig, tmp_path: Path
    ) -> None:
        # Defensive: if a future ib-async or non-conforming fake omits
        # reqMarketDataType, run_cycle must continue without raising.
        class _BareIb(_FakeIb):
            reqMarketDataType = None  # type: ignore[assignment]

        ib = _BareIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=10
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        result = asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert len(result.successful_markets) == 2

    def test_run_cycle_market_data_type_value_from_config(self, tmp_path: Path) -> None:
        # Operator overrides oi_market_data_type=1 (LIVE) when they have
        # a real-time subscription; the worker forwards the override
        # to the IBKR connection unchanged.
        config = BarSyncConfig(
            markets={"TLT": PHASE1_UNIVERSE_METADATA["TLT"]},
            data_root=tmp_path,
            bars_per_fetch=1,
            ibkr_call_timeout_seconds=2.0,
            ibkr_connect_timeout_seconds=2.0,
            oi_market_data_type=1,
        )
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=10
            ),
        ]
        worker = BarSyncWorker(
            config=config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert ib.req_market_data_type_calls == [1]

    def test_run_cycle_disconnects_even_on_partial_failure(
        self, small_config: BarSyncConfig
    ) -> None:
        ib = _FakeIb()
        ib.historical_bars = []  # both markets get empty bars
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert ib.disconnect_calls == 1


class TestBarSyncWorkerAlertSeam:
    """Task 4 — consecutive-cycle counters + P2 alert hook seam."""

    @pytest.fixture
    def small_config(self, tmp_path: Path) -> BarSyncConfig:
        # Two-market universe with /MCL specifically so the sentinel
        # path is exercisable in tests.
        return BarSyncConfig(
            markets={
                "/MES": PHASE1_UNIVERSE_METADATA["/MES"],
                "/MCL": PHASE1_UNIVERSE_METADATA["/MCL"],
            },
            data_root=tmp_path,
            bars_per_fetch=5,
            tick_interval_seconds=0.01,
            ibkr_call_timeout_seconds=2.0,
            ibkr_connect_timeout_seconds=2.0,
            oi_wait_seconds=0.1,
        )

    @staticmethod
    def _ib_with_real_bars(oi_value: Any) -> _FakeIb:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19),
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=10,
            ),
        ]
        ib.contract_details = [
            _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
        ]
        ib.oi_value_to_serve = oi_value
        return ib

    @staticmethod
    def _hook_capture() -> tuple[list[Any], Any]:
        captured: list[Any] = []

        async def hook(descriptor: Any) -> None:
            captured.append(descriptor)

        return captured, hook

    def test_default_hook_is_none(self, small_config: BarSyncConfig) -> None:
        worker = BarSyncWorker(config=small_config)
        # Internal state surface — locks contract that the seam is wired
        # but defaults to logger-only behavior.
        assert worker._partial_cycle_alert_hook is None
        assert worker._consecutive_failure_count == 0
        assert worker._consecutive_sentinel_count == 0

    def test_clean_cycle_keeps_counters_zero(self, small_config: BarSyncConfig) -> None:
        ib = self._ib_with_real_bars(oi_value=257985.0)
        captured, hook = self._hook_capture()
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
            partial_cycle_alert_hook=hook,
        )
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert worker._consecutive_failure_count == 0
        assert worker._consecutive_sentinel_count == 0
        assert captured == []

    def test_single_failure_increments_below_threshold(
        self, small_config: BarSyncConfig
    ) -> None:
        ib = _FakeIb()
        ib.connect_should_raise = ConnectionRefusedError("ib_gateway down")
        captured, hook = self._hook_capture()
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
            partial_cycle_alert_hook=hook,
        )
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert worker._consecutive_failure_count == 1
        assert captured == []

    def test_two_consecutive_failures_dispatch_alert(
        self, small_config: BarSyncConfig
    ) -> None:
        captured, hook = self._hook_capture()
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: (
                _ib := _FakeIb(),
                setattr(_ib, "connect_should_raise", ConnectionRefusedError("down")),
            )[0],
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
            partial_cycle_alert_hook=hook,
        )

        # Cycle 1: failure (counter=1, no alert)
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert worker._consecutive_failure_count == 1
        assert captured == []

        # Cycle 2: failure (counter=2, alert fires)
        asyncio.run(worker.run_cycle(today=date(2026, 5, 20)))
        assert worker._consecutive_failure_count == 2
        assert len(captured) == 1
        assert captured[0].category == "data_quality_reject"
        assert captured[0].severity == "P2"

    def test_failure_then_clean_cycle_resets_counter(
        self, small_config: BarSyncConfig
    ) -> None:
        captured, hook = self._hook_capture()
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: (
                _ib := _FakeIb(),
                setattr(_ib, "connect_should_raise", ConnectionRefusedError("down")),
            )[0],
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
            partial_cycle_alert_hook=hook,
        )

        # Cycle 1: failure
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert worker._consecutive_failure_count == 1

        # Cycle 2: swap to a clean-ib factory
        clean_ib = self._ib_with_real_bars(oi_value=257985.0)
        worker._ib_factory = lambda: clean_ib  # type: ignore[method-assign]
        asyncio.run(worker.run_cycle(today=date(2026, 5, 20)))
        assert worker._consecutive_failure_count == 0
        assert captured == []  # no alert ever fired

    def test_sentinel_substitution_alert_fires_at_threshold(
        self, small_config: BarSyncConfig
    ) -> None:
        # /MCL gets oi_value=0.0 → sentinel substitution. /MES gets real
        # OI. So the cycle is "clean failures-wise" but
        # sentinel-substituted-wise.
        captured, hook = self._hook_capture()

        def make_ib() -> _FakeIb:
            ib = _FakeIb()
            ib.historical_bars = [
                _FakeBarData(
                    date=date(2026, 5, 19),
                    open=100,
                    high=101,
                    low=99,
                    close=100.5,
                    volume=10,
                ),
            ]
            ib.contract_details = [
                _FakeContractDetails(_FakeContract(lastTradeDateOrContractMonth="20260620")),
            ]
            ib.oi_value_to_serve = 0.0  # both markets sentinel-substitute
            return ib

        worker = BarSyncWorker(
            config=small_config,
            ib_factory=make_ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
            partial_cycle_alert_hook=hook,
        )

        # Cycle 1
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        assert worker._consecutive_sentinel_count == 1
        assert worker._consecutive_failure_count == 0
        assert captured == []

        # Cycle 2
        asyncio.run(worker.run_cycle(today=date(2026, 5, 20)))
        assert worker._consecutive_sentinel_count == 2
        assert len(captured) == 1
        assert captured[0].category == "data_quality_quarantine"

    def test_no_hook_logs_dropped_at_threshold(
        self, small_config: BarSyncConfig
    ) -> None:
        # Without a hook, the descriptor MUST be logged via
        # bar_sync_alert_dropped_no_hook so the operator can grep for
        # the pattern.
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: (
                _ib := _FakeIb(),
                setattr(_ib, "connect_should_raise", ConnectionRefusedError("down")),
            )[0],
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
            # no partial_cycle_alert_hook
        )
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        with capture_logs() as logs:
            asyncio.run(worker.run_cycle(today=date(2026, 5, 20)))
        dropped = [e for e in logs if e.get("event") == "bar_sync_alert_dropped_no_hook"]
        assert len(dropped) == 1
        assert dropped[0]["severity"] == "P2"
        assert dropped[0]["category"] == "data_quality_reject"

    def test_hook_exception_logged_and_swallowed(
        self, small_config: BarSyncConfig
    ) -> None:
        # A Discord outage on the api hook side must NOT wedge the
        # cycle. Hook raises → exception is caught + logged as
        # bar_sync_alert_dispatch_failed → run_cycle returns cleanly.
        calls = 0

        async def boom_hook(descriptor: Any) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("discord went down")

        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: (
                _ib := _FakeIb(),
                setattr(_ib, "connect_should_raise", ConnectionRefusedError("down")),
            )[0],
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
            partial_cycle_alert_hook=boom_hook,
        )
        asyncio.run(worker.run_cycle(today=date(2026, 5, 19)))
        with capture_logs() as logs:
            asyncio.run(worker.run_cycle(today=date(2026, 5, 20)))
        assert calls == 1
        failed = [
            e for e in logs if e.get("event") == "bar_sync_alert_dispatch_failed"
        ]
        assert len(failed) == 1


class TestBarSyncWorkerScheduling:
    @pytest.fixture
    def small_config(self, tmp_path: Path) -> BarSyncConfig:
        return BarSyncConfig(
            markets={"TLT": PHASE1_UNIVERSE_METADATA["TLT"]},
            data_root=tmp_path,
            bars_per_fetch=1,
            tick_interval_seconds=0.01,
        )

    def test_maybe_fire_before_sync_time_noop(self, small_config: BarSyncConfig) -> None:
        ib = _FakeIb()
        # 14:00 ET = 18:00 UTC — well before 17:00 ET sync time
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 18, 0, tzinfo=UTC),
        )
        result = asyncio.run(worker.maybe_fire())
        assert result is None
        assert ib.disconnect_calls == 0
        assert worker.last_fired_session_date_et is None

    def test_maybe_fire_at_sync_time_fires(self, small_config: BarSyncConfig) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(
                date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=10
            ),
        ]
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),  # 17:00 ET
        )
        result = asyncio.run(worker.maybe_fire())
        assert result is not None
        assert worker.last_fired_session_date_et == date(2026, 5, 19)

    def test_maybe_fire_already_fired_today_skips(self, small_config: BarSyncConfig) -> None:
        ib = _FakeIb()
        ib.historical_bars = [
            _FakeBarData(date=date(2026, 5, 19), open=100, high=101, low=99, close=100.5, volume=1),
        ]
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 22, 0, tzinfo=UTC),  # 18:00 ET
            initial_fired_date=date(2026, 5, 19),
        )
        result = asyncio.run(worker.maybe_fire())
        assert result is None
        assert ib.disconnect_calls == 0

    def test_run_forever_exits_on_stop(self, small_config: BarSyncConfig) -> None:
        # Force the schedule to never fire by anchoring the clock pre-sync-time.
        ib = _FakeIb()
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 14, 0, tzinfo=UTC),  # 10:00 ET
        )

        async def _drive() -> None:
            task = asyncio.create_task(worker.run_forever())
            # Yield once so run_forever enters its loop.
            await asyncio.sleep(0.02)
            worker.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(_drive())
        assert worker.is_running is False

    def test_marks_fired_even_on_connect_failure(self, small_config: BarSyncConfig) -> None:
        # A connect-failed cycle still marks the day as fired so the
        # next tick within the same ET calendar day doesn't retry-storm.
        # Operator restarts api to reset the fired-marker.
        ib = _FakeIb()
        ib.connect_should_raise = ConnectionRefusedError("ib_gateway down")
        worker = BarSyncWorker(
            config=small_config,
            ib_factory=lambda: ib,
            clock=lambda: datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        )
        asyncio.run(worker.maybe_fire())
        assert worker.last_fired_session_date_et == date(2026, 5, 19)


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_all_surface(self) -> None:
        expected_surface = {
            "DEFAULT_BAR_SYNC_CLIENT_ID",
            "DEFAULT_BARS_PER_FETCH",
            "DEFAULT_DATA_ROOT",
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
        }
        assert set(bar_sync.__all__) == expected_surface

    def test_bar_is_frozen(self) -> None:
        b = _make_bar()
        with pytest.raises(Exception):  # FrozenInstanceError
            b.close = Decimal("999")  # type: ignore[misc]

    def test_market_sync_result_is_frozen(self) -> None:
        r = MarketSyncResult(
            market="TLT",
            success=True,
            bars_written=1,
            last_session_date=date(2026, 5, 19),
            front_month_expiry=None,
            error=None,
        )
        with pytest.raises(Exception):
            r.success = False  # type: ignore[misc]

    def test_market_sync_result_open_interest_defaults_none(self) -> None:
        # Backward compatibility: existing callers that don't pass
        # open_interest get the None default (ETF semantics).
        r = MarketSyncResult(
            market="TLT",
            success=True,
            bars_written=1,
            last_session_date=date(2026, 5, 19),
            front_month_expiry=None,
            error=None,
        )
        assert r.open_interest is None

    def test_market_sync_result_open_interest_was_sentinel_defaults_false(self) -> None:
        # Task 4 follow-up field. Defaults to False so all existing
        # callers (and the connect-failed synthesizer) get the safe
        # "not substituted" default without an explicit kwarg.
        r = MarketSyncResult(
            market="TLT",
            success=True,
            bars_written=1,
            last_session_date=date(2026, 5, 19),
            front_month_expiry=None,
            error=None,
        )
        assert r.open_interest_was_sentinel is False

    def test_bar_sync_config_default_universe_is_phase1(self) -> None:
        cfg = BarSyncConfig()
        assert set(cfg.markets.keys()) == set(PHASE1_UNIVERSE_METADATA.keys())
