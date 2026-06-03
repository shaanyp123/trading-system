"""Unit tests for ``scripts/operator_tools/trigger_v1_cycle.py``.

A22 binds: tests exercise argparse + bar parsing + payload builders +
orchestration against fakes. Zero testcontainers, zero real Postgres /
api / IBKR. The script's docstring documents this contract.

Coverage matrix:

* Argparse — every CLI arg, every default; default --dry-run=True;
  --no-dry-run flip; --allow-non-paper gate on live-env writes;
  YYYY-MM-DD validation on --session-date.
* compute_active_universe (pure) — V1_CANDIDATE_UNIVERSE minus
  V1_SIDELINED_MARKETS; /MCL never appears in the result.
* read_etf_bars_from_zip — round-trip against fixture ETF zips that
  mirror bar_sync's deci-cent integer-scaled equity-daily format.
* _parse_universe_file_bar — parses one bar_sync universe file (header
  + single data row) into a Bar; covers malformed-row / sentinel-close
  / negative-volume edge cases.
* read_futures_bars_from_universe_files — round-trip against fixture
  per-day universe file directories that mirror bar_sync's
  ``future/<market_dir>/universes/<lower>/<YYYYMMDD>.csv`` layout;
  covers lookback-window filtering, ``as_of`` upper bound, missing
  directory, non-CSV distractors, decimal precision, and the 200+ bar
  invariant for MA_SLOW=200 to clear.
* is_bar_series_stale (pure) — newest-bar-date vs threshold math.
* build_v1_parameters_from_dict (pure) — JSONB → V1Parameters round-trip
  + V1_DEFAULTS fallback on empty input.
* build_position_from_row (pure) — quantity-sign → direction; ET date
  conversion for opened_at.
* build_minimal_sizing_trace + build_signal_post_payload — payload
  shape matches what api's LeanEventRequest expects.
* emit_signal_with_dedup — dedup_skipped; dry_run_skipped; happy-path
  posted; post_failed paths.
* _amain — risk_state gate (NORMAL passes; HALT_NEW / CONVALESCENT
  block); bearer missing in non-dry-run; full happy-path with stubs.

Implementation strategy: per-test stubs constructed inline (matches the
``test_recovery_agent.py`` pattern). Bar zip fixtures are tmp_path-scoped
zipfiles built with stdlib ``zipfile`` to avoid coupling tests to
``services.data.bar_sync``'s writer functions (which would pull the full
ib_async / sqlalchemy import surface).
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from scripts.operator_tools.trigger_v1_cycle import (
    DEFAULT_API_BASE_URL,
    EXIT_BAD_ARGS,
    EXIT_BEARER_MISSING,
    EXIT_DB_INIT_FAILED,
    EXIT_OK,
    EXIT_POST_FAILED,
    EXIT_RISK_STATE_BLOCKED,
    STRATEGY_VERSION_MARKER,
    CycleSummary,
    ParsedArgs,
    SignalPostOutcome,
    _amain,
    _coerce_bool_param,
    _compute_exit_code,
    _parse_args,
    _parse_universe_file_bar,
    _summarize,
    build_exit_signal_post_payload,
    build_minimal_sizing_trace,
    build_position_from_row,
    build_signal_post_payload,
    build_v1_parameters_from_dict,
    compute_active_universe,
    emit_exit_signal_with_dedup,
    emit_signal_with_dedup,
    is_bar_series_stale,
    read_etf_bars_from_zip,
    read_futures_bars_from_universe_files,
)
from strategies.v1_trend_following.parameters import (
    V1_CANDIDATE_UNIVERSE,
    V1_DEFAULTS,
    V1_SIDELINED_MARKETS,
    default_v1_parameters,
)
from strategies.v1_trend_following.signals import (
    Bar,
    CandidateSignal,
    Direction,
    RejectionReason,
)

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _make_signal(
    *,
    market: str = "TLT",
    direction: Direction = Direction.LONG,
    decision_price: Decimal = Decimal("100.00"),
    stop_price: Decimal = Decimal("95.00"),
) -> CandidateSignal:
    """Construct a CandidateSignal with a fully populated indicators_snapshot.

    Mirrors what V1TrendFollowing.generate_signals returns; tests use
    this so they don't have to run the whole strategy pipeline when only
    payload shape matters.
    """
    return CandidateSignal(
        market=market,
        direction=direction,
        signal_type="donchian_breakout",
        session_date=date(2026, 5, 26),
        decision_price=decision_price,
        stop_price=stop_price,
        indicators_snapshot={
            "donchian_high": Decimal("99.50"),
            "donchian_low": Decimal("90.00"),
            "ma_fast": Decimal("96.00"),
            "ma_slow": Decimal("92.00"),
            "efficiency_ratio": Decimal("0.65"),
            "atr": Decimal("1.50"),
            "lookback_days_donchian": 60,
        },
    )


def _write_etf_bar_zip(
    zip_path: Path,
    *,
    ticker: str,
    bars: Iterable[tuple[date, Decimal]],
) -> None:
    """Write an ETF-shaped zip that matches bar_sync's equity-daily format.

    Prices are passed as Decimal close values (open/high/low all set to
    close ± 1) and the test writer applies the deci-cent integer
    scaling that bar_sync produces. The reader function then has to
    decode that scaling back to Decimal — round-tripping pins the
    contract.
    """
    scale = 10000
    lines = []
    for d, close in bars:
        open_v = int((close - Decimal("0.50")) * scale)
        high_v = int((close + Decimal("0.50")) * scale)
        low_v = int((close - Decimal("1.00")) * scale)
        close_v = int(close * scale)
        lines.append(f"{d:%Y%m%d} 00:00,{open_v},{high_v},{low_v},{close_v},10000")
    body = ("\n".join(lines) + "\n").encode()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{ticker.lower()}.csv", body)


def _write_futures_universe_files(
    universes_dir: Path,
    *,
    bars: Iterable[tuple[date, Decimal]],
    expiry: str = "202606",
) -> None:
    """Write one per-day universe file per (date, close) tuple.

    Mirrors ``services/data/bar_sync.py::build_futures_universe_csv``:

        #expiry,open,high,low,close,volume,open_interest
        <expiry>,<O>,<H>,<L>,<C>,<V>,<OI>

    Each file lives at ``<universes_dir>/<YYYYMMDD>.csv``. The reader
    picks the per-day file matching each session_date in its lookback
    window. ``expiry`` is the front-month tag stamped into each row;
    tests can vary it across calls to simulate roll boundaries.
    """
    universes_dir.mkdir(parents=True, exist_ok=True)
    for d, close in bars:
        open_v = close - Decimal("0.50")
        high_v = close + Decimal("0.50")
        low_v = close - Decimal("1.00")
        body = (
            "#expiry,open,high,low,close,volume,open_interest\n"
            f"{expiry},{open_v},{high_v},{low_v},{close},10000,200000\n"
        ).encode()
        (universes_dir / f"{d:%Y%m%d}.csv").write_bytes(body)


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_minimal_paper_args_default_to_dry_run_today_session_date(self) -> None:
        from zoneinfo import ZoneInfo

        args = _parse_args(["--env", "paper"])
        assert args.env == "paper"
        # Safety: default dry-run is True (operator must opt out)
        assert args.dry_run is True
        # session_date defaults to today's ET calendar date — clock-tick
        # races around midnight could put us ±1 day, so accept the window.
        today_et = datetime.now(tz=UTC).astimezone(ZoneInfo("America/New_York")).date()
        assert abs((args.session_date - today_et).days) <= 1
        assert args.api_base_url == DEFAULT_API_BASE_URL

    def test_env_required_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_env_unknown_value_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "production"])

    def test_no_dry_run_flag_flips_safety_off(self) -> None:
        args = _parse_args(["--env", "paper", "--no-dry-run"])
        assert args.dry_run is False

    def test_session_date_explicit_passes_through(self) -> None:
        args = _parse_args(["--env", "paper", "--session-date", "2026-05-26"])
        assert args.session_date == date(2026, 5, 26)

    def test_session_date_invalid_format_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "paper", "--session-date", "2026/05/26"])

    def test_live_small_without_allow_non_paper_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "live-small"])

    def test_live_small_with_allow_non_paper_passes(self) -> None:
        args = _parse_args(["--env", "live-small", "--allow-non-paper"])
        assert args.env == "live-small"
        assert args.allow_non_paper is True

    def test_live_scale_without_allow_non_paper_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "live-scale"])

    def test_data_root_override_passes_through(self) -> None:
        args = _parse_args(["--env", "paper", "--data-root", "/tmp/custom-lean-data"])
        assert args.data_root == Path("/tmp/custom-lean-data")

    def test_api_url_override_passes_through(self) -> None:
        args = _parse_args(["--env", "paper", "--api-url", "http://stub:1234"])
        assert args.api_base_url == "http://stub:1234"

    def test_http_timeout_zero_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "paper", "--http-timeout-seconds", "0"])


# ---------------------------------------------------------------------------
# compute_active_universe (pure)
# ---------------------------------------------------------------------------


class TestComputeActiveUniverse:
    def test_mcl_never_appears_in_active_set(self) -> None:
        active = compute_active_universe()
        assert "/MCL" not in active
        # All sidelined markets are filtered out (set difference invariant)
        for sidelined in V1_SIDELINED_MARKETS:
            assert sidelined not in active

    def test_active_set_is_candidate_minus_sidelined(self) -> None:
        active = set(compute_active_universe())
        candidate = set(V1_CANDIDATE_UNIVERSE)
        sidelined = set(V1_SIDELINED_MARKETS)
        assert active == candidate - sidelined

    def test_active_set_preserves_candidate_order(self) -> None:
        active = compute_active_universe()
        candidate_order = [m for m in V1_CANDIDATE_UNIVERSE if m not in V1_SIDELINED_MARKETS]
        assert list(active) == candidate_order


# ---------------------------------------------------------------------------
# Bar reading
# ---------------------------------------------------------------------------


class TestReadEtfBarsFromZip:
    def test_round_trip_decodes_deci_cent_prices(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "tlt.zip"
        _write_etf_bar_zip(
            zip_path,
            ticker="TLT",
            bars=[
                (date(2026, 5, 24), Decimal("85.50")),
                (date(2026, 5, 25), Decimal("86.00")),
                (date(2026, 5, 26), Decimal("85.75")),
            ],
        )
        bars = read_etf_bars_from_zip(zip_path, ticker="TLT")
        assert len(bars) == 3
        # Bars sorted ascending
        assert [b.session_date for b in bars] == [
            date(2026, 5, 24),
            date(2026, 5, 25),
            date(2026, 5, 26),
        ]
        # Deci-cent scaling decoded exactly
        assert bars[0].close == Decimal("85.5000")
        assert bars[1].close == Decimal("86.0000")
        assert bars[2].close == Decimal("85.7500")

    def test_missing_zip_returns_empty(self, tmp_path: Path) -> None:
        bars = read_etf_bars_from_zip(tmp_path / "missing.zip", ticker="TLT")
        assert bars == []

    def test_zip_without_expected_member_returns_empty(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "tlt.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("wrong_name.csv", b"junk")
        bars = read_etf_bars_from_zip(zip_path, ticker="TLT")
        assert bars == []


class TestParseUniverseFileBar:
    def test_well_formed_file_parses_to_bar(self) -> None:
        body = (
            b"#expiry,open,high,low,close,volume,open_interest\n"
            b"202606,29450,29747.75,29437.5,29558.75,2008229,267668\n"
        )
        bar = _parse_universe_file_bar(body, session_date=date(2026, 5, 22))
        assert bar is not None
        assert bar.session_date == date(2026, 5, 22)
        assert bar.open == Decimal("29450")
        assert bar.high == Decimal("29747.75")
        assert bar.low == Decimal("29437.5")
        assert bar.close == Decimal("29558.75")
        assert bar.volume == 2008229

    def test_header_only_returns_none(self) -> None:
        body = b"#expiry,open,high,low,close,volume,open_interest\n"
        assert _parse_universe_file_bar(body, session_date=date(2026, 5, 22)) is None

    def test_close_negative_one_sentinel_returns_none(self) -> None:
        body = b"#expiry,open,high,low,close,volume,open_interest\n202606,100,101,99,-1,1000,0\n"
        assert _parse_universe_file_bar(body, session_date=date(2026, 5, 22)) is None

    def test_malformed_row_returns_none(self) -> None:
        body = (
            b"#expiry,open,high,low,close,volume,open_interest\n"
            b"202606,not-a-number,29747.75,29437.5,29558.75,2008229,267668\n"
        )
        assert _parse_universe_file_bar(body, session_date=date(2026, 5, 22)) is None

    def test_negative_volume_coerced_to_zero(self) -> None:
        body = b"#expiry,open,high,low,close,volume,open_interest\n202606,100,101,99,100.5,-1,0\n"
        bar = _parse_universe_file_bar(body, session_date=date(2026, 5, 22))
        assert bar is not None
        assert bar.volume == 0


class TestReadFuturesBarsFromUniverseFiles:
    def test_reads_all_files_in_lookback_window(self, tmp_path: Path) -> None:
        universes_dir = tmp_path / "mnq"
        _write_futures_universe_files(
            universes_dir,
            bars=[
                (date(2026, 5, 24), Decimal("29400")),
                (date(2026, 5, 25), Decimal("29500")),
                (date(2026, 5, 26), Decimal("29558.75")),
            ],
        )
        bars = read_futures_bars_from_universe_files(
            universes_dir, ticker="MNQ", as_of=date(2026, 5, 26)
        )
        assert len(bars) == 3
        # Bars sorted ascending
        assert [b.session_date for b in bars] == [
            date(2026, 5, 24),
            date(2026, 5, 25),
            date(2026, 5, 26),
        ]
        assert bars[-1].close == Decimal("29558.75")

    def test_filters_files_outside_lookback(self, tmp_path: Path) -> None:
        universes_dir = tmp_path / "mnq"
        _write_futures_universe_files(
            universes_dir,
            bars=[
                # Older than as_of - 365 days → should be filtered out
                (date(2024, 1, 1), Decimal("18000")),
                # Inside window
                (date(2025, 9, 1), Decimal("25000")),
                (date(2026, 5, 26), Decimal("29558")),
            ],
        )
        bars = read_futures_bars_from_universe_files(
            universes_dir,
            ticker="MNQ",
            as_of=date(2026, 5, 26),
            lookback_days=365,
        )
        assert [b.session_date for b in bars] == [
            date(2025, 9, 1),
            date(2026, 5, 26),
        ]

    def test_filters_files_after_as_of(self, tmp_path: Path) -> None:
        """Files dated AFTER session_date are excluded — replay must use
        only what bar_sync would have seen by that date.
        """
        universes_dir = tmp_path / "mnq"
        _write_futures_universe_files(
            universes_dir,
            bars=[
                (date(2026, 5, 24), Decimal("29400")),
                (date(2026, 5, 25), Decimal("29500")),
                # Past as_of → should be excluded
                (date(2026, 5, 26), Decimal("29558")),
                (date(2026, 5, 27), Decimal("29600")),
            ],
        )
        bars = read_futures_bars_from_universe_files(
            universes_dir, ticker="MNQ", as_of=date(2026, 5, 25)
        )
        assert [b.session_date for b in bars] == [
            date(2026, 5, 24),
            date(2026, 5, 25),
        ]

    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        bars = read_futures_bars_from_universe_files(
            tmp_path / "does_not_exist",
            ticker="MNQ",
            as_of=date(2026, 5, 26),
        )
        assert bars == []

    def test_non_csv_files_ignored(self, tmp_path: Path) -> None:
        universes_dir = tmp_path / "mnq"
        universes_dir.mkdir()
        # Valid universe file
        (universes_dir / "20260526.csv").write_bytes(
            b"#expiry,open,high,low,close,volume,open_interest\n202606,100,101,99,100.5,1000,500\n"
        )
        # Distractor files that shouldn't be read
        (universes_dir / "README.md").write_text("not a universe file")
        (universes_dir / "20260526.txt").write_text("wrong extension")
        (universes_dir / "garbage").write_text("no extension")
        bars = read_futures_bars_from_universe_files(
            universes_dir, ticker="MNQ", as_of=date(2026, 5, 26)
        )
        assert len(bars) == 1
        assert bars[0].session_date == date(2026, 5, 26)

    def test_unparseable_filename_skipped(self, tmp_path: Path) -> None:
        universes_dir = tmp_path / "mnq"
        universes_dir.mkdir()
        # Valid filename
        (universes_dir / "20260526.csv").write_bytes(
            b"#expiry,open,high,low,close,volume,open_interest\n202606,100,101,99,100.5,1000,500\n"
        )
        # Unparseable filename pattern
        (universes_dir / "not-a-date.csv").write_text("garbage")
        (universes_dir / "2026-05-26.csv").write_text("wrong format")
        bars = read_futures_bars_from_universe_files(
            universes_dir, ticker="MNQ", as_of=date(2026, 5, 26)
        )
        assert len(bars) == 1
        assert bars[0].session_date == date(2026, 5, 26)

    def test_decimal_precision_preserved(self, tmp_path: Path) -> None:
        universes_dir = tmp_path / "mnq"
        _write_futures_universe_files(
            universes_dir,
            bars=[(date(2026, 5, 26), Decimal("29558.7525"))],
        )
        bars = read_futures_bars_from_universe_files(
            universes_dir, ticker="MNQ", as_of=date(2026, 5, 26)
        )
        # Decimal precision preserved through the CSV write+read cycle
        assert bars[0].close == Decimal("29558.7525")

    def test_provides_enough_bars_for_ma_slow_200(self, tmp_path: Path) -> None:
        """The whole point of the fix: 200+ bars available so MA_SLOW_DAYS=200
        clears. v1 of this tool (PR #248) hit insufficient_bar_history because
        the front-month zip member only had ~175 bars; universe files give us
        the full continuous-mapped history.
        """
        universes_dir = tmp_path / "mes"
        # 280 weekday session-date bars going back from 2026-05-26.
        # Use a simple weekday-only iteration to mirror real CME data.
        bars_to_write: list[tuple[date, Decimal]] = []
        d = date(2026, 5, 26)
        i = 0
        while len(bars_to_write) < 280:
            if d.weekday() < 5:  # Mon-Fri
                bars_to_write.append((d, Decimal(str(5000 + i))))
                i += 1
            d -= timedelta(days=1)
        _write_futures_universe_files(universes_dir, bars=bars_to_write)
        bars = read_futures_bars_from_universe_files(
            universes_dir, ticker="MES", as_of=date(2026, 5, 26)
        )
        assert len(bars) >= 200, f"Need >= 200 bars for MA_SLOW=200 to clear; got {len(bars)}"


# ---------------------------------------------------------------------------
# Bar staleness (pure)
# ---------------------------------------------------------------------------


class TestIsBarSeriesStale:
    def test_empty_bars_treated_as_stale(self) -> None:
        assert is_bar_series_stale(bars=[], as_of=date(2026, 5, 26)) is True

    def test_newest_within_threshold_returns_false(self) -> None:
        bars = [
            Bar(
                session_date=date(2026, 5, 25),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=10,
            )
        ]
        # 1 day stale; default threshold is 5 → not stale
        assert is_bar_series_stale(bars=bars, as_of=date(2026, 5, 26)) is False

    def test_newest_beyond_threshold_returns_true(self) -> None:
        bars = [
            Bar(
                session_date=date(2026, 5, 1),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=10,
            )
        ]
        # 25 days stale; default threshold is 5 → stale
        assert is_bar_series_stale(bars=bars, as_of=date(2026, 5, 26)) is True

    def test_threshold_override_inclusive_at_boundary(self) -> None:
        bars = [
            Bar(
                session_date=date(2026, 5, 20),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=10,
            )
        ]
        # Exactly 6 days stale; threshold=6 → boundary (newest > 6 false)
        assert is_bar_series_stale(bars=bars, as_of=date(2026, 5, 26), threshold_days=6) is False
        # threshold=5 → 6 > 5 → stale
        assert is_bar_series_stale(bars=bars, as_of=date(2026, 5, 26), threshold_days=5) is True


# ---------------------------------------------------------------------------
# build_v1_parameters_from_dict (pure)
# ---------------------------------------------------------------------------


class TestBuildV1Parameters:
    def test_empty_dict_falls_back_to_defaults(self) -> None:
        params = build_v1_parameters_from_dict({})
        assert params == default_v1_parameters()

    def test_canonical_keys_round_trip(self) -> None:
        # V1_DEFAULTS uses canonical SCREAMING_SNAKE_CASE keys + a mix of
        # int + str values, mirroring the JSONB column shape.
        raw: dict[str, object] = dict(V1_DEFAULTS)
        params = build_v1_parameters_from_dict(raw)
        assert params.lookback_days_donchian == 60
        assert params.ma_fast_days == 50
        assert params.ma_slow_days == 200
        assert params.efficiency_ratio_threshold == Decimal("0.20")
        assert params.atr_lookback_days == 20
        assert params.min_holding_days == 14

    def test_missing_required_key_raises_key_error(self) -> None:
        raw: dict[str, object] = dict(V1_DEFAULTS)
        del raw["MA_SLOW_DAYS"]
        with pytest.raises(KeyError):
            build_v1_parameters_from_dict(raw)


# ---------------------------------------------------------------------------
# build_position_from_row (pure)
# ---------------------------------------------------------------------------


class TestBuildPositionFromRow:
    def test_positive_quantity_is_long(self) -> None:
        p = build_position_from_row(
            market="TLT",
            quantity=10,
            avg_cost=Decimal("85.50"),
            opened_at_utc=None,
        )
        assert p.direction == Direction.LONG
        assert p.quantity == 10
        assert p.opened_at_session_date is None

    def test_negative_quantity_is_short(self) -> None:
        p = build_position_from_row(
            market="TLT",
            quantity=-5,
            avg_cost=Decimal("85.50"),
            opened_at_utc=None,
        )
        assert p.direction == Direction.SHORT

    def test_zero_quantity_is_flat(self) -> None:
        p = build_position_from_row(
            market="TLT",
            quantity=0,
            avg_cost=Decimal("0"),
            opened_at_utc=None,
        )
        assert p.direction == Direction.FLAT

    def test_opened_at_utc_converted_to_et_calendar_date(self) -> None:
        # 2026-05-26 03:00 UTC = 2026-05-25 23:00 ET (EDT, UTC-4) → prior day in ET
        utc_ts = datetime(2026, 5, 26, 3, 0, tzinfo=UTC)
        p = build_position_from_row(
            market="TLT",
            quantity=10,
            avg_cost=Decimal("85.50"),
            opened_at_utc=utc_ts,
        )
        assert p.opened_at_session_date == date(2026, 5, 25)

    def test_flat_position_ignores_opened_at(self) -> None:
        p = build_position_from_row(
            market="TLT",
            quantity=0,
            avg_cost=Decimal("0"),
            opened_at_utc=datetime(2026, 5, 26, 12, 0, tzinfo=UTC),
        )
        # Flat positions don't have a meaningful open date
        assert p.opened_at_session_date is None


# ---------------------------------------------------------------------------
# build_minimal_sizing_trace (pure)
# ---------------------------------------------------------------------------


class TestBuildMinimalSizingTrace:
    def test_payload_shape_matches_lean(self) -> None:
        signal = _make_signal()
        trace = build_minimal_sizing_trace(
            signal=signal,
            target_contracts=1,
            equity=Decimal("20000.00"),
        )
        # Shape mirrors lean/v1_strategy.py::_build_minimal_sizing_trace
        assert trace["schema_version"] == 1
        assert "stage_0_universe" in trace
        assert "lean_naive_sizing" in trace
        stage0 = trace["stage_0_universe"]
        assert stage0["active_markets"] == [signal.market]
        assert stage0["excluded"] == []
        # Strategy inputs are Decimal-as-string per A05
        si = stage0["strategy_inputs"][signal.market]
        assert si["donchian_high"] == "99.50"
        assert si["ma_fast"] == "96.00"
        assert si["efficiency_ratio"] == "0.65"
        assert si["lookback_days_donchian"] == 60
        # lean_naive_sizing surfaces the equity + rationale
        sizing = trace["lean_naive_sizing"]
        assert sizing["target_contracts"] == 1
        assert sizing["equity_usd"] == "20000.00"
        assert "operator-trigger" in sizing["rationale"].lower()


# ---------------------------------------------------------------------------
# build_signal_post_payload (pure)
# ---------------------------------------------------------------------------


class TestBuildSignalPostPayload:
    def test_payload_contains_all_required_signal_emitted_fields(self) -> None:
        signal = _make_signal()
        payload = build_signal_post_payload(
            signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            target_contracts=1,
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
        )
        # Required by services/api/routes/internal/lean.py post_lean_signal
        for required in (
            "event_type",
            "ts_utc",
            "algorithm_id",
            "market",
            "direction",
            "target_contracts",
            "decision_price",
            "sizing_trace",
            "strategy_version",
        ):
            assert required in payload, f"missing {required!r}"
        # Heartbeat-extras (session_date_et + equity_usd + live_mode) also
        # included for parity with LEAN's payload
        assert payload["session_date_et"] == "2026-05-26"
        assert payload["equity_usd"] == "20000.00"
        assert payload["live_mode"] is True
        # event_type literal
        assert payload["event_type"] == "signal_emitted"
        # decision_price serialized as string per A05
        assert payload["decision_price"] == "100.00"
        # strategy_version uses the tool-trigger marker (forensic visibility)
        assert payload["strategy_version"] == STRATEGY_VERSION_MARKER


# ---------------------------------------------------------------------------
# emit_signal_with_dedup (orchestration)
# ---------------------------------------------------------------------------


class _StubResponse:
    """Minimal httpx.Response stub — exposes status_code + .json()."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _StubAsyncClient:
    """Minimal httpx.AsyncClient stub — captures POST calls + returns canned responses."""

    def __init__(self, response: _StubResponse | Exception) -> None:
        self._response = response
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
        self.posts.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def aclose(self) -> None:
        return None


class TestEmitSignalWithDedup:
    @pytest.mark.asyncio
    async def test_market_in_dedup_set_short_circuits(self) -> None:
        signal = _make_signal(market="TLT")
        client = _StubAsyncClient(_StubResponse(202, {}))
        outcome = await emit_signal_with_dedup(
            signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted=frozenset({"TLT"}),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=False,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "dedup_skipped"
        assert outcome.market == "TLT"
        # No POST attempted
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_dry_run_logs_but_skips_post(self) -> None:
        signal = _make_signal(market="TLT")
        client = _StubAsyncClient(_StubResponse(202, {}))
        outcome = await emit_signal_with_dedup(
            signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted=frozenset(),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=True,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "dry_run_skipped"
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_post_succeeded_surfaces_signal_id(self) -> None:
        signal_id = str(uuid4())
        audit_uuid = str(uuid4())
        signal = _make_signal(market="MES")
        client = _StubAsyncClient(
            _StubResponse(
                202,
                {"signal_id": signal_id, "audit_event_uuid": audit_uuid},
            )
        )
        outcome = await emit_signal_with_dedup(
            signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted=frozenset(),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=False,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "posted"
        assert outcome.signal_id == signal_id
        assert outcome.audit_event_uuid == audit_uuid
        assert outcome.http_status_code == 202
        # POST shape
        assert len(client.posts) == 1
        sent = client.posts[0]
        assert sent["url"].endswith("/api/internal/lean/signals")
        assert sent["headers"]["Authorization"] == "Bearer fake-token"
        assert sent["json"]["market"] == "MES"
        assert sent["json"]["event_type"] == "signal_emitted"

    @pytest.mark.asyncio
    async def test_post_non_2xx_surfaces_post_failed(self) -> None:
        signal = _make_signal(market="IEF")
        client = _StubAsyncClient(_StubResponse(422, {"error_code": "LEAN_SIGNAL_PAYLOAD_INVALID"}))
        outcome = await emit_signal_with_dedup(
            signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted=frozenset(),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=False,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "post_failed"
        assert outcome.http_status_code == 422
        assert "422" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_network_failure_surfaces_post_failed(self) -> None:
        signal = _make_signal(market="SHY")
        client = _StubAsyncClient(httpx.ConnectError("connection refused"))
        outcome = await emit_signal_with_dedup(
            signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted=frozenset(),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=False,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "post_failed"
        # http_status_code is None on network failure (no response received)
        assert outcome.http_status_code is None
        assert "ConnectError" in (outcome.error or "")


# ---------------------------------------------------------------------------
# _summarize + _compute_exit_code (pure)
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_empty_outcomes_produce_zero_counts(self) -> None:
        s = _summarize(
            outcomes=[],
            rejections=[],
            history_missing_markets=(),
            stale_markets=(),
        )
        assert s.signals_generated == 0
        assert s.signals_emitted == 0
        assert s.post_failed == 0
        assert s.rejections_count == 0

    def test_rejection_reasons_grouped(self) -> None:
        s = _summarize(
            outcomes=[],
            rejections=[
                ("TLT", RejectionReason.NO_BREAKOUT),
                ("IEF", RejectionReason.NO_BREAKOUT),
                ("SHY", RejectionReason.EFFICIENCY_BELOW_THRESHOLD),
            ],
            history_missing_markets=(),
            stale_markets=(),
        )
        assert s.rejections_count == 3
        assert s.rejection_reasons["no_breakout"] == 2
        assert s.rejection_reasons["efficiency_below_threshold"] == 1

    def test_mixed_outcomes_counted_correctly(self) -> None:
        outcomes = [
            SignalPostOutcome(market="TLT", status="posted", http_status_code=202),
            SignalPostOutcome(market="IEF", status="dedup_skipped"),
            SignalPostOutcome(market="SHY", status="dry_run_skipped"),
            SignalPostOutcome(market="TIP", status="post_failed", http_status_code=500),
        ]
        s = _summarize(
            outcomes=outcomes,
            rejections=[],
            history_missing_markets=(),
            stale_markets=(),
        )
        assert s.signals_generated == 4
        assert s.signals_emitted == 1
        assert s.dedup_skipped == 1
        assert s.dry_run_skipped == 1
        assert s.post_failed == 1


class TestComputeExitCode:
    def test_dry_run_always_returns_ok(self) -> None:
        s = CycleSummary(
            signals_generated=5,
            signals_emitted=0,
            dedup_skipped=0,
            dry_run_skipped=5,
            post_failed=0,
            rejections_count=0,
            rejection_reasons={},
            history_missing_markets=(),
            stale_markets=(),
        )
        assert _compute_exit_code(s, dry_run=True) == EXIT_OK

    def test_clean_run_returns_ok(self) -> None:
        s = CycleSummary(
            signals_generated=3,
            signals_emitted=3,
            dedup_skipped=0,
            dry_run_skipped=0,
            post_failed=0,
            rejections_count=0,
            rejection_reasons={},
            history_missing_markets=(),
            stale_markets=(),
        )
        assert _compute_exit_code(s, dry_run=False) == EXIT_OK

    def test_any_post_failure_returns_exit_post_failed(self) -> None:
        s = CycleSummary(
            signals_generated=3,
            signals_emitted=2,
            dedup_skipped=0,
            dry_run_skipped=0,
            post_failed=1,
            rejections_count=0,
            rejection_reasons={},
            history_missing_markets=(),
            stale_markets=(),
        )
        assert _compute_exit_code(s, dry_run=False) == EXIT_POST_FAILED


# ---------------------------------------------------------------------------
# _amain integration with stubbed session_factory + http
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal AsyncSession stub for trigger_v1_cycle.

    Routes each SELECT statement to a canned response dict keyed by a
    substring match against the SQL string. Tests construct the
    response map per scenario.
    """

    def __init__(self, *, query_results: dict[str, Any]) -> None:
        self._query_results = query_results
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql_str = str(statement)
        self.queries.append((sql_str, params or {}))
        for needle, result in self._query_results.items():
            if needle in sql_str:
                return result
        # Default: empty result
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])


def _result_one(row: Any) -> Any:
    """Build a SQLAlchemy-result-like object whose fetchone() returns ``row``."""
    return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: [row] if row else [])


def _result_many(rows: list[Any]) -> Any:
    return SimpleNamespace(fetchone=lambda: rows[0] if rows else None, fetchall=lambda: rows)


def _build_session_factory_for_context(*, risk_state: str) -> Any:
    """Build a session_factory that returns a FakeSession with a configured risk_state.

    All other queries return empty / sensible defaults so the cycle can
    run without bar-on-disk dependencies (we override args.data_root to
    a tmp_path with no zips, so the cycle generates 0 signals).

    PR-B (2026-05-26): there are now TWO ``FROM signals`` queries — the
    entry-side dedup (broad) and the exit-side dedup (status-filtered).
    Order matters in the matcher because the first key whose substring is
    found in the SQL string wins. The exit-side key must come BEFORE the
    entry-side key so its narrower predicate matches the exit dedup query.
    """
    account_id = uuid4()
    query_results: dict[str, Any] = {
        # accounts → one active row
        "FROM accounts": _result_one(SimpleNamespace(id=account_id)),
        # risk_state → caller-configured
        "FROM risk_state": _result_one(SimpleNamespace(state=risk_state)),
        # parameter_sets → empty (falls back to V1_DEFAULTS)
        "FROM parameter_sets": _result_one(None),
        # positions_current → empty
        "FROM positions_current": _result_many([]),
        # signals (exit-side dedup, status-filtered per PR-B). MUST come
        # before "FROM signals" so the narrower needle matches first.
        "signal_type = 'exit'": _result_many([]),
        # signals (entry-side dedup) → empty
        "FROM signals": _result_many([]),
        # balances → no row
        "FROM balances": _result_one(None),
    }

    def factory() -> _FakeSession:
        return _FakeSession(query_results=query_results)

    return factory


@pytest.fixture
def empty_data_root(tmp_path: Path) -> Path:
    """Return an empty lean_data root; all market reads will return [] bars.

    Strategy then generates zero signals — useful for testing the
    risk-state gate + dry-run path without needing to construct 250-bar
    fixtures that would actually fire a breakout.
    """
    (tmp_path / "equity" / "usa" / "daily").mkdir(parents=True, exist_ok=True)
    # Universe-files dir (futures) — read by the new
    # read_futures_bars_from_universe_files path.
    (tmp_path / "future" / "cme" / "universes" / "mes").mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestAmainRiskStateGate:
    @pytest.mark.asyncio
    async def test_normal_risk_state_proceeds(self, empty_data_root: Path) -> None:
        args = ParsedArgs(
            env="paper",
            session_date=date(2026, 5, 26),
            dry_run=True,
            data_root=empty_data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        sf = _build_session_factory_for_context(risk_state="NORMAL")
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
            bearer_token_override=None,
        )
        # No bars on disk → 0 signals → exit OK in dry-run
        assert result == EXIT_OK

    @pytest.mark.asyncio
    async def test_halt_new_risk_state_aborts(self, empty_data_root: Path) -> None:
        args = ParsedArgs(
            env="paper",
            session_date=date(2026, 5, 26),
            dry_run=True,
            data_root=empty_data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        sf = _build_session_factory_for_context(risk_state="HALT_NEW")
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
            bearer_token_override=None,
        )
        assert result == EXIT_RISK_STATE_BLOCKED

    @pytest.mark.asyncio
    async def test_convalescent_risk_state_also_aborts(self, empty_data_root: Path) -> None:
        """The dispatcher permits CONVALESCENT for normal signal flow, but
        this tool is stricter — it requires NORMAL specifically. The
        operator brief said: 'abort with a clear message if not NORMAL'.
        """
        args = ParsedArgs(
            env="paper",
            session_date=date(2026, 5, 26),
            dry_run=True,
            data_root=empty_data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        sf = _build_session_factory_for_context(risk_state="CONVALESCENT")
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
            bearer_token_override=None,
        )
        assert result == EXIT_RISK_STATE_BLOCKED


class TestAmainBearerTokenGate:
    @pytest.mark.asyncio
    async def test_missing_bearer_in_non_dry_run_returns_exit_bearer_missing(
        self,
        empty_data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        args = ParsedArgs(
            env="paper",
            session_date=date(2026, 5, 26),
            dry_run=False,
            data_root=empty_data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        sf = _build_session_factory_for_context(risk_state="NORMAL")
        monkeypatch.delenv("LEAN_LOCAL_BEARER_TOKEN", raising=False)
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
            bearer_token_override=None,
        )
        assert result == EXIT_BEARER_MISSING

    @pytest.mark.asyncio
    async def test_missing_bearer_in_dry_run_is_allowed(
        self,
        empty_data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        args = ParsedArgs(
            env="paper",
            session_date=date(2026, 5, 26),
            dry_run=True,
            data_root=empty_data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        sf = _build_session_factory_for_context(risk_state="NORMAL")
        monkeypatch.delenv("LEAN_LOCAL_BEARER_TOKEN", raising=False)
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
            bearer_token_override=None,
        )
        # Dry-run bypasses the bearer check entirely
        assert result == EXIT_OK


class TestAmainNoActiveAccount:
    @pytest.mark.asyncio
    async def test_no_active_account_returns_db_init_failed(self, empty_data_root: Path) -> None:
        args = ParsedArgs(
            env="paper",
            session_date=date(2026, 5, 26),
            dry_run=True,
            data_root=empty_data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        # Override accounts query to return no rows
        query_results = {
            "FROM accounts": _result_one(None),
        }

        def sf() -> _FakeSession:
            return _FakeSession(query_results=query_results)

        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
            bearer_token_override=None,
        )
        assert result == EXIT_DB_INIT_FAILED


# ---------------------------------------------------------------------------
# End-to-end: real bars on disk + dry-run path
# ---------------------------------------------------------------------------


def _emit_breakout_bars(start: date, n_bars: int) -> list[tuple[date, Decimal]]:
    """Generate ``n_bars`` (date, close) tuples that pass the V1 entry filters.

    Same trajectory shape as ``test_strategy_v1._series_with_breakout``:
    smooth 0.2%/day uptrend (an efficient one-directional move → high
    Efficiency Ratio) with a small modulation, plus a clear breakout above
    the trailing high on the final bar.
    """
    base = Decimal("100")
    out: list[tuple[date, Decimal]] = []
    for i in range(n_bars):
        drift = base * (Decimal("1.002") ** i)
        wave = Decimal(str(0.5 + 0.5 * ((i % 7) / 6)))
        close = (drift + wave).quantize(Decimal("0.01"))
        out.append((start + timedelta(days=i), close))
    # Final-bar breakout — significantly above the trailing 60-day high
    out[-1] = (out[-1][0], Decimal("250.00"))
    return out


@pytest.fixture
def lean_data_root_with_breakout(tmp_path: Path) -> tuple[Path, date]:
    """Lay down ETF zips + futures universe-file directories for the
    full active universe with bars that will trigger LONG breakouts.
    Returns the data root + the as_of session_date (= the last bar's
    date).

    Mirrors bar_sync's on-disk layout exactly:
      * ``equity/usa/daily/<lower>.zip`` for ETFs
      * ``future/<market_dir>/universes/<lower>/<YYYYMMDD>.csv`` for futures
        (one universe file per session_date, replicating LEAN's
        DataMappingMode.OPEN_INTEREST resolver input)
    """
    n_bars = 250
    start = date(2025, 9, 1)
    bars = _emit_breakout_bars(start, n_bars)
    as_of = bars[-1][0]

    # ETF bundles
    for ticker in ("TLT", "IEF", "SHY", "TIP"):
        _write_etf_bar_zip(
            tmp_path / "equity" / "usa" / "daily" / f"{ticker.lower()}.zip",
            ticker=ticker,
            bars=bars,
        )
    # Futures: per-day universe files (keys + market_dir mirror PHASE1_UNIVERSE_METADATA)
    futures = [
        ("MES", "cme"),
        ("MNQ", "cme"),
        ("MYM", "cbot"),
        ("M2K", "cme"),
        ("MGC", "comex"),
        ("MBT", "cme"),
    ]
    for ticker, market_dir in futures:
        _write_futures_universe_files(
            tmp_path / "future" / market_dir / "universes" / ticker.lower(),
            bars=bars,
        )
    return tmp_path, as_of


class TestAmainEndToEndDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_with_breakout_bars_logs_would_post_no_real_post(
        self,
        lean_data_root_with_breakout: tuple[Path, date],
    ) -> None:
        data_root, as_of = lean_data_root_with_breakout
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=True,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        sf = _build_session_factory_for_context(risk_state="NORMAL")
        # http_client should never be used in dry-run, but pass a stub
        client = _StubAsyncClient(_StubResponse(202, {}))
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=client,  # type: ignore[arg-type]
            bearer_token_override=None,
        )
        assert result == EXIT_OK
        # No actual POSTs in dry-run
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_dedup_skips_already_emitted_markets(
        self,
        lean_data_root_with_breakout: tuple[Path, date],
    ) -> None:
        """When the dedup query returns a market, that market's signal
        is skipped even though the strategy generates it.
        """
        data_root, as_of = lean_data_root_with_breakout
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=False,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        account_id = uuid4()
        # Return TLT in the entry-side dedup-query response → that market
        # gets skipped on the entry pipeline. Exit dedup is empty so any
        # held positions could still emit exits (but the fixture seeds no
        # held positions, so the exit pipeline emits nothing).
        already_emitted_rows = [SimpleNamespace(market="TLT")]
        query_results: dict[str, Any] = {
            "FROM accounts": _result_one(SimpleNamespace(id=account_id)),
            "FROM risk_state": _result_one(SimpleNamespace(state="NORMAL")),
            "FROM parameter_sets": _result_one(None),
            "FROM positions_current": _result_many([]),
            # Exit-side dedup (status-filtered) — narrower needle must come
            # first per the matcher's first-match-wins iteration order.
            "signal_type = 'exit'": _result_many([]),
            "FROM signals": _result_many(already_emitted_rows),
            "FROM balances": _result_one(SimpleNamespace(net_liquidation=20000)),
        }

        def sf() -> _FakeSession:
            return _FakeSession(query_results=query_results)

        client = _StubAsyncClient(
            _StubResponse(
                202,
                {"signal_id": str(uuid4()), "audit_event_uuid": str(uuid4())},
            )
        )
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=client,  # type: ignore[arg-type]
            bearer_token_override="fake-bearer",
        )
        # POSTs should have happened for non-TLT markets only (since TLT
        # was deduped). Number of POSTs is < total active universe.
        assert result == EXIT_OK
        # TLT should NOT appear in any POST payload
        posted_markets = {p["json"]["market"] for p in client.posts}
        assert "TLT" not in posted_markets


# ---------------------------------------------------------------------------
# PR-B (2026-05-26) — exit-pipeline integration via the operator tool.
# ---------------------------------------------------------------------------


def _make_exit_signal(
    *,
    market: str = "TLT",
    exit_reason: str = "trend_flip",
    prior_position_direction: Direction = Direction.LONG,
    prior_position_quantity: int = 3,
    decision_price: Decimal = Decimal("100.00"),
    paired_entry_market: str | None = None,
    indicators_snapshot: dict | None = None,
) -> CandidateSignal:
    """Construct an exit CandidateSignal mirroring generate_exit_candidates output."""
    snapshot = indicators_snapshot if indicators_snapshot is not None else {}
    return CandidateSignal(
        market=market,
        direction=Direction.FLAT,
        signal_type="exit",
        session_date=date(2026, 5, 26),
        decision_price=decision_price,
        stop_price=Decimal("0"),
        indicators_snapshot=snapshot,
        exit_reason=exit_reason,  # type: ignore[arg-type]
        prior_position_direction=prior_position_direction,
        prior_position_quantity=prior_position_quantity,
        paired_entry_market=paired_entry_market,
    )


class TestParseArgsExitFlags:
    """PR-B CLI flag matrix per design §Q6."""

    def test_default_neither_exits_only_nor_entries_only(self) -> None:
        args = _parse_args(["--env", "paper"])
        assert args.exits_only is False
        assert args.entries_only is False
        assert args.reason_filter == ()

    def test_exits_only_flag(self) -> None:
        args = _parse_args(["--env", "paper", "--exits-only"])
        assert args.exits_only is True
        assert args.entries_only is False

    def test_entries_only_flag(self) -> None:
        args = _parse_args(["--env", "paper", "--entries-only"])
        assert args.entries_only is True
        assert args.exits_only is False

    def test_exits_only_and_entries_only_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "paper", "--exits-only", "--entries-only"])

    def test_reason_filter_single(self) -> None:
        args = _parse_args(["--env", "paper", "--reason-filter", "decommission"])
        assert args.reason_filter == ("decommission",)

    def test_reason_filter_multiple(self) -> None:
        args = _parse_args(
            [
                "--env",
                "paper",
                "--reason-filter",
                "decommission",
                "--reason-filter",
                "reversal",
            ]
        )
        assert args.reason_filter == ("decommission", "reversal")

    def test_reason_filter_invalid_value_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "paper", "--reason-filter", "bogus"])


class TestCoerceBoolParam:
    """The JSONB / string → bool coercion for STRATEGY_DECOMMISSIONED."""

    def test_none_returns_false(self) -> None:
        assert _coerce_bool_param(None) is False

    def test_true_string_returns_true(self) -> None:
        assert _coerce_bool_param("True") is True
        assert _coerce_bool_param("true") is True
        assert _coerce_bool_param("TRUE") is True
        # Tolerates surrounding whitespace
        assert _coerce_bool_param("  true  ") is True

    def test_false_string_returns_false(self) -> None:
        assert _coerce_bool_param("False") is False
        assert _coerce_bool_param("false") is False
        assert _coerce_bool_param("") is False
        assert _coerce_bool_param("anything else") is False

    def test_bool_passthrough(self) -> None:
        assert _coerce_bool_param(True) is True
        assert _coerce_bool_param(False) is False

    def test_int_nonzero_is_true(self) -> None:
        assert _coerce_bool_param(1) is True
        assert _coerce_bool_param(0) is False


class TestBuildV1ParametersExitFields:
    """PR-B: STRATEGY_DECOMMISSIONED + EXIT_AUTO_APPROVE are now read from JSONB."""

    def test_missing_exit_fields_default_to_false(self) -> None:
        raw: dict[str, object] = {
            k: v
            for k, v in V1_DEFAULTS.items()
            if k not in ("STRATEGY_DECOMMISSIONED", "EXIT_AUTO_APPROVE")
        }
        params = build_v1_parameters_from_dict(raw)
        assert params.strategy_decommissioned is False
        assert params.exit_auto_approve is False

    def test_strategy_decommissioned_true_propagates(self) -> None:
        raw: dict[str, object] = dict(V1_DEFAULTS)
        raw["STRATEGY_DECOMMISSIONED"] = True
        params = build_v1_parameters_from_dict(raw)
        assert params.strategy_decommissioned is True

    def test_strategy_decommissioned_string_true_propagates(self) -> None:
        """JSONB driver may return strings for bool values written as strings."""
        raw: dict[str, object] = dict(V1_DEFAULTS)
        raw["STRATEGY_DECOMMISSIONED"] = "True"
        params = build_v1_parameters_from_dict(raw)
        assert params.strategy_decommissioned is True

    def test_exit_auto_approve_true_propagates(self) -> None:
        raw: dict[str, object] = dict(V1_DEFAULTS)
        raw["EXIT_AUTO_APPROVE"] = True
        params = build_v1_parameters_from_dict(raw)
        assert params.exit_auto_approve is True

    def test_default_v1_parameters_matches_safe_defaults(self) -> None:
        """V1_DEFAULTS includes both flags as False (SAFE)."""
        params = build_v1_parameters_from_dict(dict(V1_DEFAULTS))
        assert params.strategy_decommissioned is False
        assert params.exit_auto_approve is False


class TestBuildExitSignalPostPayload:
    """The exit-side payload shape mirrors LeanEventRequest's exit branch."""

    def test_trend_flip_payload_has_required_exit_fields(self) -> None:
        signal = _make_exit_signal(
            indicators_snapshot={
                "ma_fast": Decimal("96.00"),
                "ma_slow": Decimal("92.00"),
                "last_close": Decimal("95.00"),
                "atr": Decimal("1.50"),
            },
        )
        payload = build_exit_signal_post_payload(
            exit_signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
        )
        assert payload["event_type"] == "signal_emitted"
        assert payload["signal_type"] == "exit"
        assert payload["direction"] == "flat"
        assert payload["exit_reason"] == "trend_flip"
        assert payload["prior_position_direction"] == "long"
        assert payload["prior_position_quantity"] == 3
        assert payload["target_contracts"] == 0
        assert payload["decision_price"] == "100.00"
        # paired_entry_market omitted for trend_flip
        assert "paired_entry_market" not in payload
        # sizing_trace carries the rationale + prior position state
        sizing = payload["sizing_trace"]
        assert sizing["lean_naive_sizing"]["exit_reason"] == "trend_flip"
        assert sizing["lean_naive_sizing"]["prior_position_direction"] == "long"

    def test_reversal_payload_carries_paired_entry_market(self) -> None:
        signal = _make_exit_signal(
            exit_reason="reversal",
            paired_entry_market="MES",
            indicators_snapshot={},  # reversal exits have no snapshot
        )
        payload = build_exit_signal_post_payload(
            exit_signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
        )
        assert payload["exit_reason"] == "reversal"
        assert payload["paired_entry_market"] == "MES"
        # Empty indicators snapshot in sizing_trace
        sizing = payload["sizing_trace"]
        assert sizing["stage_0_universe"]["strategy_inputs"]["TLT"] == {}

    def test_decommission_short_position(self) -> None:
        signal = _make_exit_signal(
            exit_reason="decommission",
            prior_position_direction=Direction.SHORT,
            prior_position_quantity=-5,
        )
        payload = build_exit_signal_post_payload(
            exit_signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
        )
        assert payload["exit_reason"] == "decommission"
        assert payload["prior_position_direction"] == "short"
        assert payload["prior_position_quantity"] == -5


class TestEmitExitSignalWithDedup:
    """Exit-side per-signal orchestration mirrors emit_signal_with_dedup."""

    @pytest.mark.asyncio
    async def test_market_in_dedup_set_short_circuits(self) -> None:
        signal = _make_exit_signal(market="TLT")
        client = _StubAsyncClient(_StubResponse(202, {}))
        outcome = await emit_exit_signal_with_dedup(
            exit_signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted_exits=frozenset({"TLT"}),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=False,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "dedup_skipped"
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_dry_run_skips_post(self) -> None:
        signal = _make_exit_signal(market="TLT")
        client = _StubAsyncClient(_StubResponse(202, {}))
        outcome = await emit_exit_signal_with_dedup(
            exit_signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted_exits=frozenset(),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=True,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "dry_run_skipped"
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_post_succeeds_carries_signal_type_exit(self) -> None:
        signal = _make_exit_signal(market="MES")
        signal_id = str(uuid4())
        audit_uuid = str(uuid4())
        client = _StubAsyncClient(
            _StubResponse(202, {"signal_id": signal_id, "audit_event_uuid": audit_uuid})
        )
        outcome = await emit_exit_signal_with_dedup(
            exit_signal=signal,
            session_date=date(2026, 5, 26),
            equity=Decimal("20000.00"),
            already_emitted_exits=frozenset(),
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=datetime(2026, 5, 26, 21, 30, tzinfo=UTC),
            dry_run=False,
            http_client=client,  # type: ignore[arg-type]
            api_base_url=DEFAULT_API_BASE_URL,
            bearer_token="fake-token",
        )
        assert outcome.status == "posted"
        assert outcome.signal_id == signal_id
        # Verify the POSTed body carried signal_type='exit'
        assert len(client.posts) == 1
        sent = client.posts[0]["json"]
        assert sent["signal_type"] == "exit"
        assert sent["exit_reason"] == "trend_flip"
        assert sent["prior_position_direction"] == "long"
        assert sent["prior_position_quantity"] == 3


class TestSummarizeExits:
    """_summarize now folds exit_outcomes + exit_rejections into the same struct."""

    def test_exit_outcomes_counted(self) -> None:
        exit_outcomes = [
            SignalPostOutcome(market="TLT", status="posted", http_status_code=202),
            SignalPostOutcome(market="MES", status="dry_run_skipped"),
            SignalPostOutcome(market="IEF", status="dedup_skipped"),
            SignalPostOutcome(market="MNQ", status="post_failed", http_status_code=500),
        ]
        s = _summarize(
            outcomes=[],
            rejections=[],
            history_missing_markets=(),
            stale_markets=(),
            exit_outcomes=exit_outcomes,
            exit_rejections=[("TIP", RejectionReason.TREND_HOLDS)],
        )
        assert s.exits_generated == 4
        assert s.exits_emitted == 1
        assert s.exits_dedup_skipped == 1
        assert s.exits_dry_run_skipped == 1
        assert s.exits_post_failed == 1
        assert s.exit_rejections_count == 1
        assert s.exit_rejection_reasons["trend_holds"] == 1
        # Unified post_failed: entry + exit failures sum
        assert s.post_failed == 1

    def test_unified_post_failed_sums_entry_plus_exit(self) -> None:
        outcomes = [SignalPostOutcome(market="TLT", status="post_failed")]
        exit_outcomes = [SignalPostOutcome(market="MES", status="post_failed")]
        s = _summarize(
            outcomes=outcomes,
            rejections=[],
            history_missing_markets=(),
            stale_markets=(),
            exit_outcomes=exit_outcomes,
        )
        assert s.post_failed == 2  # 1 entry + 1 exit
        assert s.exits_post_failed == 1


class TestAmainExitFlagMatrix:
    """End-to-end: --exits-only / --entries-only flow through _amain."""

    @pytest.mark.asyncio
    async def test_entries_only_skips_exit_pipeline(
        self,
        lean_data_root_with_breakout: tuple[Path, date],
    ) -> None:
        """--entries-only: even if positions exist, no exit POSTs fire."""
        data_root, as_of = lean_data_root_with_breakout
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=True,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
            entries_only=True,
        )
        sf = _build_session_factory_for_context(risk_state="NORMAL")
        client = _StubAsyncClient(_StubResponse(202, {}))
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=client,  # type: ignore[arg-type]
            bearer_token_override=None,
        )
        assert result == EXIT_OK
        # Dry-run + no real POSTs
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_exits_only_skips_entry_pipeline_emissions(
        self,
        lean_data_root_with_breakout: tuple[Path, date],
    ) -> None:
        """--exits-only: entry candidates evaluated but not POSTed."""
        data_root, as_of = lean_data_root_with_breakout
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=False,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
            exits_only=True,
        )
        sf = _build_session_factory_for_context(risk_state="NORMAL")
        client = _StubAsyncClient(_StubResponse(202, {}))
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=client,  # type: ignore[arg-type]
            bearer_token_override="fake-bearer",
        )
        # No held positions in the fixture → 0 entries, 0 exits → exit_ok.
        # The point is no entries are POSTed even though the breakout fires.
        assert result == EXIT_OK
        # Zero entry POSTs (no positions = no exits either, but the key
        # invariant is no entries on --exits-only)
        entry_posts = [p for p in client.posts if p["json"].get("signal_type") == "entry"]
        assert entry_posts == []


def _emit_held_position_bars(start: date, n_bars: int) -> list[tuple[date, Decimal]]:
    """Emit bars that DON'T fire a breakout but DO satisfy MA_SLOW=200.

    Used in tests that need positions held (mocked) without the entry
    pipeline firing breakouts at the same time. Sideways trajectory.
    """
    base = Decimal("100")
    out: list[tuple[date, Decimal]] = []
    for i in range(n_bars):
        # Drift around base; small noise; no breakout
        wave = Decimal(str((i % 7 - 3) * 0.1))
        out.append((start + timedelta(days=i), base + wave))
    return out


@pytest.fixture
def lean_data_root_sideways(tmp_path: Path) -> tuple[Path, date]:
    """Bars that won't trigger entries — useful for isolating exit tests."""
    n_bars = 250
    start = date(2025, 9, 1)
    bars = _emit_held_position_bars(start, n_bars)
    as_of = bars[-1][0]
    for ticker in ("TLT", "IEF", "SHY", "TIP"):
        _write_etf_bar_zip(
            tmp_path / "equity" / "usa" / "daily" / f"{ticker.lower()}.zip",
            ticker=ticker,
            bars=bars,
        )
    futures = [
        ("MES", "cme"),
        ("MNQ", "cme"),
        ("MYM", "cbot"),
        ("M2K", "cme"),
        ("MGC", "comex"),
        ("MBT", "cme"),
    ]
    for ticker, market_dir in futures:
        _write_futures_universe_files(
            tmp_path / "future" / market_dir / "universes" / ticker.lower(),
            bars=bars,
        )
    return tmp_path, as_of


class TestAmainKillSwitchCeremony:
    """STRATEGY_DECOMMISSIONED=True drives a decommission CLOSE per held position.

    This exercises the operator's kill-switch ceremony end-to-end via the
    operator tool (the equivalent path for LEAN runs inside a container +
    is not testable directly here per the mypy/ruff exclusion).
    """

    @pytest.mark.asyncio
    async def test_decommission_emits_exit_for_each_held(
        self,
        lean_data_root_sideways: tuple[Path, date],
    ) -> None:
        data_root, as_of = lean_data_root_sideways
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=False,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        account_id = uuid4()
        # Two held positions — both should emit a decommission exit.
        # MES held LONG +2, TLT held SHORT -10. opened_at_utc set well
        # before as_of so MIN_HOLDING_DAYS is satisfied (though
        # decommission overrides that gate anyway).
        position_rows = [
            SimpleNamespace(
                market="/MES",
                quantity=2,
                avg_cost=Decimal("5000"),
                opened_at_utc=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            ),
            SimpleNamespace(
                market="TLT",
                quantity=-10,
                avg_cost=Decimal("85"),
                opened_at_utc=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            ),
        ]
        # parameter_sets row carries STRATEGY_DECOMMISSIONED=True
        param_row = SimpleNamespace(parameters={**V1_DEFAULTS, "STRATEGY_DECOMMISSIONED": True})
        query_results: dict[str, Any] = {
            "FROM accounts": _result_one(SimpleNamespace(id=account_id)),
            "FROM risk_state": _result_one(SimpleNamespace(state="NORMAL")),
            "FROM parameter_sets": _result_one(param_row),
            "FROM positions_current": _result_many(position_rows),
            "signal_type = 'exit'": _result_many([]),
            "FROM signals": _result_many([]),
            "FROM balances": _result_one(SimpleNamespace(net_liquidation=20000)),
        }

        def sf() -> _FakeSession:
            return _FakeSession(query_results=query_results)

        client = _StubAsyncClient(
            _StubResponse(
                202,
                {"signal_id": str(uuid4()), "audit_event_uuid": str(uuid4())},
            )
        )
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=client,  # type: ignore[arg-type]
            bearer_token_override="fake-bearer",
        )
        assert result == EXIT_OK
        # Both held markets received a decommission exit POST
        exit_posts = [p["json"] for p in client.posts if p["json"].get("signal_type") == "exit"]
        exit_markets = {p["market"] for p in exit_posts}
        assert exit_markets == {"/MES", "TLT"}
        for p in exit_posts:
            assert p["exit_reason"] == "decommission"
            assert p["direction"] == "flat"
            assert p["target_contracts"] == 0
        # MES prior_position_direction='long', qty=2; TLT short, qty=-10
        by_market = {p["market"]: p for p in exit_posts}
        assert by_market["/MES"]["prior_position_direction"] == "long"
        assert by_market["/MES"]["prior_position_quantity"] == 2
        assert by_market["TLT"]["prior_position_direction"] == "short"
        assert by_market["TLT"]["prior_position_quantity"] == -10

    @pytest.mark.asyncio
    async def test_decommission_with_reason_filter_only_decommission_emits(
        self,
        lean_data_root_sideways: tuple[Path, date],
    ) -> None:
        """--reason-filter=decommission lets decommission exits through;
        a non-matching reason in the filter would block them."""
        data_root, as_of = lean_data_root_sideways
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=False,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
            reason_filter=("reversal",),  # decommission NOT in filter
        )
        account_id = uuid4()
        position_rows = [
            SimpleNamespace(
                market="/MES",
                quantity=2,
                avg_cost=Decimal("5000"),
                opened_at_utc=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            ),
        ]
        param_row = SimpleNamespace(parameters={**V1_DEFAULTS, "STRATEGY_DECOMMISSIONED": True})
        query_results: dict[str, Any] = {
            "FROM accounts": _result_one(SimpleNamespace(id=account_id)),
            "FROM risk_state": _result_one(SimpleNamespace(state="NORMAL")),
            "FROM parameter_sets": _result_one(param_row),
            "FROM positions_current": _result_many(position_rows),
            "signal_type = 'exit'": _result_many([]),
            "FROM signals": _result_many([]),
            "FROM balances": _result_one(SimpleNamespace(net_liquidation=20000)),
        }

        def sf() -> _FakeSession:
            return _FakeSession(query_results=query_results)

        client = _StubAsyncClient(_StubResponse(202, {}))
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=client,  # type: ignore[arg-type]
            bearer_token_override="fake-bearer",
        )
        assert result == EXIT_OK
        # decommission exits filtered out by --reason-filter=reversal
        exit_posts = [p["json"] for p in client.posts if p["json"].get("signal_type") == "exit"]
        assert exit_posts == []


class TestExitDedupReEmitsAfterRejection:
    """Status-filtered dedup re-emits exits rejected/cancelled/expired."""

    @pytest.mark.asyncio
    async def test_exit_already_in_pending_blocks_re_emit(
        self,
        lean_data_root_sideways: tuple[Path, date],
    ) -> None:
        """An exit already in non-terminal status blocks re-emission."""
        data_root, as_of = lean_data_root_sideways
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=False,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        account_id = uuid4()
        position_rows = [
            SimpleNamespace(
                market="/MES",
                quantity=2,
                avg_cost=Decimal("5000"),
                opened_at_utc=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            ),
        ]
        param_row = SimpleNamespace(parameters={**V1_DEFAULTS, "STRATEGY_DECOMMISSIONED": True})
        # exit-side dedup query returns /MES — the prior exit is pending/
        # approved/working etc., so the tool MUST NOT re-emit.
        query_results: dict[str, Any] = {
            "FROM accounts": _result_one(SimpleNamespace(id=account_id)),
            "FROM risk_state": _result_one(SimpleNamespace(state="NORMAL")),
            "FROM parameter_sets": _result_one(param_row),
            "FROM positions_current": _result_many(position_rows),
            "signal_type = 'exit'": _result_many([SimpleNamespace(market="/MES")]),
            "FROM signals": _result_many([]),
            "FROM balances": _result_one(SimpleNamespace(net_liquidation=20000)),
        }

        def sf() -> _FakeSession:
            return _FakeSession(query_results=query_results)

        client = _StubAsyncClient(_StubResponse(202, {}))
        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=client,  # type: ignore[arg-type]
            bearer_token_override="fake-bearer",
        )
        assert result == EXIT_OK
        # No exit POST because /MES dedup matched
        exit_posts = [p["json"] for p in client.posts if p["json"].get("signal_type") == "exit"]
        assert exit_posts == []

    @pytest.mark.asyncio
    async def test_dedup_query_filters_by_signal_type_and_status(
        self,
        lean_data_root_sideways: tuple[Path, date],
    ) -> None:
        """Verify the SQL issued by the exit dedup has signal_type='exit'
        AND status NOT IN ('rejected','cancelled','expired') predicates.
        """
        data_root, as_of = lean_data_root_sideways
        args = ParsedArgs(
            env="paper",
            session_date=as_of,
            dry_run=True,
            data_root=data_root,
            api_base_url=DEFAULT_API_BASE_URL,
            http_timeout_seconds=15.0,
            allow_non_paper=False,
        )
        account_id = uuid4()
        captured_queries: list[str] = []

        class _CapturingFakeSession(_FakeSession):
            async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
                captured_queries.append(str(statement))
                return await super().execute(statement, params)

        query_results: dict[str, Any] = {
            "FROM accounts": _result_one(SimpleNamespace(id=account_id)),
            "FROM risk_state": _result_one(SimpleNamespace(state="NORMAL")),
            "FROM parameter_sets": _result_one(None),
            "FROM positions_current": _result_many([]),
            "signal_type = 'exit'": _result_many([]),
            "FROM signals": _result_many([]),
            "FROM balances": _result_one(None),
        }

        def sf() -> _CapturingFakeSession:
            return _CapturingFakeSession(query_results=query_results)

        result = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
            bearer_token_override=None,
        )
        assert result == EXIT_OK
        # At least one captured query carries the exit-side predicates
        exit_dedup_queries = [q for q in captured_queries if "signal_type = 'exit'" in q]
        assert exit_dedup_queries, "Expected an exit-side dedup query with signal_type filter"
        # Status filter is also present in the dedup query
        assert any(
            "rejected" in q and "cancelled" in q and "expired" in q for q in exit_dedup_queries
        )


# ---------------------------------------------------------------------------
# Main entry point exit-code mapping
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    def test_bad_args_returns_exit_bad_args(self) -> None:
        from scripts.operator_tools.trigger_v1_cycle import main

        # No --env → argparse exits with code 2; main maps to EXIT_BAD_ARGS
        result = main(["--session-date", "2026-05-26"])
        assert result == EXIT_BAD_ARGS
