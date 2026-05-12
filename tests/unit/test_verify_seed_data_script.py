"""Unit tests for ``scripts/verify_seed_data.py``.

The script reads LEAN equity-daily zips on disk and compares the close
prices against a second independent provider (Stooq or Tiingo). These
tests exercise:

- The LEAN-zip parser (``read_lean_zip``) round-trip from synthetic zips
  built in tmp_path.
- The Stooq + Tiingo CSV parsers (``parse_stooq_csv`` / ``parse_tiingo_csv``)
  against canned response bodies — empty, malformed, missing columns,
  ISO-date variants.
- The fetcher functions (``fetch_stooq`` / ``fetch_tiingo``) via a stubbed
  ``http_get`` callable so the tests never touch the network.
- The diff computation (``compute_diff``) — exact-match, sub-threshold,
  super-threshold, divide-by-zero defence, dates-only-in-one-side accounting.
- The end-to-end ``main()`` CLI exit-code contract across the 3 paths
  (clean = 0, breach = 1, fetch error = 2).

Pure Python; no network; no testcontainers. Anti-pattern [A22] does not
bind (zero audit_log emissions in this layer).
"""

from __future__ import annotations

import dataclasses
import io
import json
import urllib.error
import zipfile
from contextlib import redirect_stdout
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.verify_seed_data import (
    DEFAULT_THRESHOLD_BP,
    DEFAULT_TICKERS,
    STOOQ_URL_TEMPLATE,
    TIINGO_URL_TEMPLATE,
    TickerDiffRow,
    TickerVerification,
    compute_diff,
    fetch_stooq,
    fetch_tiingo,
    main,
    parse_stooq_csv,
    parse_tiingo_csv,
    read_lean_zip,
    verify_one,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _build_lean_zip(
    zip_path: Path,
    rows: list[tuple[str, int, int, int, int, int]],
) -> None:
    """Write a LEAN-format equity-daily zip at ``zip_path``.

    Each ``rows`` tuple is ``(YYYYMMDD, O*10000, H*10000, L*10000, C*10000, V)``.
    """
    csv_lines = [f"{ymd} 00:00,{o},{h},{lo},{c},{v}" for (ymd, o, h, lo, c, v) in rows]
    csv_bytes = ("\n".join(csv_lines) + "\n").encode("utf-8")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    inner_name = zip_path.stem + ".csv"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, csv_bytes)


def _build_lean_data_dir(
    base: Path,
    ticker_to_rows: dict[str, list[tuple[str, int, int, int, int, int]]],
) -> Path:
    """Create a ``<base>/equity/usa/daily/<lower>.zip`` layout for tests."""
    daily_dir = base / "equity" / "usa" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    for ticker, rows in ticker_to_rows.items():
        _build_lean_zip(daily_dir / f"{ticker.lower()}.zip", rows)
    return base


# A small reusable LEAN fixture: TLT for 3 trading days, closes $85.50,
# $85.60, $85.70 (LEAN deci-cents = 855000, 856000, 857000).
TLT_LEAN_ROWS = [
    ("20260101", 855000, 856500, 854500, 855000, 10_000_000),
    ("20260102", 855500, 856500, 854900, 856000, 11_000_000),
    ("20260103", 855900, 857200, 855600, 857000, 12_000_000),
]


# ---------------------------------------------------------------------------
# read_lean_zip
# ---------------------------------------------------------------------------


class TestReadLeanZip:
    def test_happy_path(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "tlt.zip"
        _build_lean_zip(zip_path, TLT_LEAN_ROWS)
        data = read_lean_zip(zip_path)
        assert data == {
            "2026-01-01": Decimal("85.5"),
            "2026-01-02": Decimal("85.6"),
            "2026-01-03": Decimal("85.7"),
        }

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_lean_zip(tmp_path / "does-not-exist.zip")

    def test_multi_entry_zip_rejected(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "tlt.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("tlt.csv", b"")
            zf.writestr("extra.csv", b"")
        with pytest.raises(ValueError, match="2 entries"):
            read_lean_zip(zip_path)

    def test_malformed_row_rejected(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "tlt.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("tlt.csv", b"20260101 00:00,1,2,3\n")  # only 4 fields
        with pytest.raises(ValueError, match="expected 6 fields"):
            read_lean_zip(zip_path)

    def test_bad_date_token_rejected(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "tlt.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("tlt.csv", b"NOTADATE 00:00,1,2,3,4,5\n")
        with pytest.raises(ValueError, match="bad date token"):
            read_lean_zip(zip_path)

    def test_handles_no_space_in_timestamp(self, tmp_path: Path) -> None:
        """The format spec uses ``YYYYMMDD 00:00`` but the parser tolerates a
        bare ``YYYYMMDD`` token for forward-compatibility."""
        zip_path = tmp_path / "tlt.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("tlt.csv", b"20260101,1,2,3,42000,5\n")
        data = read_lean_zip(zip_path)
        assert data == {"2026-01-01": Decimal("4.2")}

    def test_empty_zip_returns_empty_dict(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "tlt.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("tlt.csv", b"")
        assert read_lean_zip(zip_path) == {}

    def test_close_uses_deci_cent_scaling(self, tmp_path: Path) -> None:
        """LEAN stores ``close * 10000``; verify the parser divides back out
        to the exact Decimal dollar value (no floating-point drift)."""
        zip_path = tmp_path / "tlt.zip"
        # Close = $85.56 → 855600
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("tlt.csv", b"20260511 00:00,858800,859200,854900,855600,17699500\n")
        data = read_lean_zip(zip_path)
        assert data == {"2026-05-11": Decimal("85.56")}


# ---------------------------------------------------------------------------
# parse_stooq_csv
# ---------------------------------------------------------------------------


class TestParseStooqCsv:
    def test_happy_path(self) -> None:
        body = (
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-01,85.50,85.65,85.45,85.50,10000000\n"
            "2026-01-02,85.55,85.65,85.49,85.60,11000000\n"
        )
        data = parse_stooq_csv(body)
        assert data == {
            "2026-01-01": Decimal("85.50"),
            "2026-01-02": Decimal("85.60"),
        }

    def test_no_data_body_raises(self) -> None:
        with pytest.raises(ValueError, match="no data"):
            parse_stooq_csv("No data")

    def test_empty_body_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_stooq_csv("")

    def test_missing_close_column_raises(self) -> None:
        body = "Date,Open,High,Low,Volume\n2026-01-01,1,2,3,4\n"
        with pytest.raises(ValueError, match="Date/Close"):
            parse_stooq_csv(body)

    def test_header_only_body_raises(self) -> None:
        body = "Date,Open,High,Low,Close,Volume\n"
        with pytest.raises(ValueError, match="zero rows"):
            parse_stooq_csv(body)

    def test_skips_rows_with_empty_close(self) -> None:
        body = (
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-01,85.50,85.65,85.45,85.50,10000000\n"
            "2026-01-02,85.55,85.65,85.49,,11000000\n"
            "2026-01-03,85.55,85.65,85.49,85.70,12000000\n"
        )
        data = parse_stooq_csv(body)
        assert data == {
            "2026-01-01": Decimal("85.50"),
            "2026-01-03": Decimal("85.70"),
        }


# ---------------------------------------------------------------------------
# parse_tiingo_csv
# ---------------------------------------------------------------------------


class TestParseTiingoCsv:
    def test_happy_path_with_timestamps(self) -> None:
        body = (
            "date,close,high,low,open,volume,adjClose,adjHigh,adjLow,adjOpen,"
            "adjVolume,divCash,splitFactor\n"
            "2026-01-01T00:00:00.000Z,85.50,85.65,85.45,85.50,10000000,"
            "85.50,85.65,85.45,85.50,10000000,0.0,1.0\n"
            "2026-01-02T00:00:00.000Z,85.60,85.65,85.49,85.55,11000000,"
            "85.60,85.65,85.49,85.55,11000000,0.0,1.0\n"
        )
        data = parse_tiingo_csv(body)
        assert data == {
            "2026-01-01": Decimal("85.50"),
            "2026-01-02": Decimal("85.60"),
        }

    def test_iso_date_no_timestamp(self) -> None:
        body = "date,close,open\n2026-01-01,85.50,85.50\n2026-01-02,85.60,85.55\n"
        data = parse_tiingo_csv(body)
        assert data == {
            "2026-01-01": Decimal("85.50"),
            "2026-01-02": Decimal("85.60"),
        }

    def test_empty_body_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_tiingo_csv("")

    def test_missing_close_column_raises(self) -> None:
        with pytest.raises(ValueError, match="date/close"):
            parse_tiingo_csv("date,open\n2026-01-01,85.50\n")

    def test_uses_raw_close_not_adjusted(self) -> None:
        """The seed_lean_data.py path writes factor_files with 1,1 (no
        dividend/split adjustments). Tiingo's ``close`` is raw + ``adjClose``
        is dividend/split-adjusted; we must compare against ``close`` to
        match the on-disk convention.
        """
        body = (
            "date,close,adjClose\n"
            "2026-01-01T00:00:00.000Z,85.50,82.10\n"  # adjusted significantly lower
        )
        data = parse_tiingo_csv(body)
        assert data["2026-01-01"] == Decimal("85.50")


# ---------------------------------------------------------------------------
# fetch_stooq / fetch_tiingo URL construction + http_get injection
# ---------------------------------------------------------------------------


class TestFetchStooq:
    def test_url_construction(self) -> None:
        captured: list[str] = []

        def fake_http_get(url: str) -> str:
            captured.append(url)
            return "Date,Close\n2026-01-01,85.50\n"

        fetch_stooq(
            "TLT",
            _date(2024, 12, 2),
            _date(2026, 5, 11),
            http_get=fake_http_get,
        )
        assert captured == [
            STOOQ_URL_TEMPLATE.format(ticker_lower="tlt", d1="20241202", d2="20260511")
        ]

    def test_returns_parsed_dict(self) -> None:
        def fake_http_get(_: str) -> str:
            return "Date,Open,High,Low,Close,Volume\n2026-01-01,85,86,84,85.5,1000000\n"

        data = fetch_stooq("TLT", _date(2026, 1, 1), _date(2026, 1, 2), http_get=fake_http_get)
        assert data == {"2026-01-01": Decimal("85.5")}


class TestFetchTiingo:
    def test_url_construction(self) -> None:
        captured: list[str] = []

        def fake_http_get(url: str) -> str:
            captured.append(url)
            return "date,close\n2026-01-01,85.50\n"

        fetch_tiingo(
            "TLT",
            _date(2024, 12, 2),
            _date(2026, 5, 11),
            api_key="testkey123",
            http_get=fake_http_get,
        )
        assert captured == [
            TIINGO_URL_TEMPLATE.format(
                ticker_lower="tlt",
                start="2024-12-02",
                end="2026-05-11",
                token="testkey123",
            )
        ]

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(ValueError, match="api-key required"):
            fetch_tiingo(
                "TLT",
                _date(2026, 1, 1),
                _date(2026, 1, 2),
                api_key="",
                http_get=lambda _u: "date,close\n2026-01-01,85.50\n",
            )


# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------


class TestComputeDiff:
    def test_exact_match_zero_divergence(self) -> None:
        lean = {"2026-01-01": Decimal("85.50"), "2026-01-02": Decimal("85.60")}
        cross = {"2026-01-01": Decimal("85.50"), "2026-01-02": Decimal("85.60")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
        )
        assert v.passed
        assert v.shared_dates == 2
        assert v.rows_over_threshold == 0
        assert v.max_divergence_bp == Decimal("0")

    def test_sub_threshold_divergence_passes(self) -> None:
        """0.5bp divergence with threshold=1.0bp must pass."""
        lean = {"2026-01-01": Decimal("85.50")}
        # 85.50 vs 85.5043 → diff $0.0043 / $85.5043 ≈ 0.5026bp
        cross = {"2026-01-01": Decimal("85.5043")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
        )
        assert v.passed
        assert v.rows_over_threshold == 0
        # Sanity-check the divergence is in the 0.4-0.6bp band.
        assert Decimal("0.4") < v.max_divergence_bp < Decimal("0.6")

    def test_super_threshold_divergence_fails(self) -> None:
        lean = {"2026-01-01": Decimal("85.50"), "2026-01-02": Decimal("90.00")}
        # 85.50 vs 85.00 → ~58.8bp. 90 vs 90 → 0bp.
        cross = {"2026-01-01": Decimal("85.00"), "2026-01-02": Decimal("90.00")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
        )
        assert not v.passed
        assert v.rows_over_threshold == 1
        assert v.max_divergence_bp > Decimal("50")
        # The worst row should be 2026-01-01.
        assert v.worst_rows[0].date_iso == "2026-01-01"
        # signed_divergence_bp should be POSITIVE (LEAN > cross).
        assert v.worst_rows[0].signed_divergence_bp > 0

    def test_signed_divergence_negative_when_lean_below_cross(self) -> None:
        lean = {"2026-01-01": Decimal("85.00")}
        cross = {"2026-01-01": Decimal("85.50")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("100.0"),  # tolerate the big diff to inspect signed
            show_worst=5,
        )
        assert v.worst_rows[0].signed_divergence_bp < 0

    def test_dates_only_in_lean(self) -> None:
        lean = {"2026-01-01": Decimal("85.50"), "2026-01-02": Decimal("85.60")}
        cross = {"2026-01-01": Decimal("85.50")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
        )
        assert v.dates_only_in_lean == 1
        assert v.dates_only_in_cross == 0
        assert v.shared_dates == 1

    def test_dates_only_in_cross(self) -> None:
        lean = {"2026-01-01": Decimal("85.50")}
        cross = {
            "2026-01-01": Decimal("85.50"),
            "2025-12-31": Decimal("85.30"),
        }
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
        )
        assert v.dates_only_in_lean == 0
        assert v.dates_only_in_cross == 1

    def test_divide_by_zero_cross_close_surfaces_as_breach(self) -> None:
        """If the cross-source reports close=0 (data corruption), the
        comparison can't divide; surface as a sentinel divergence that
        always exceeds threshold so the operator sees it."""
        lean = {"2026-01-01": Decimal("85.50")}
        cross = {"2026-01-01": Decimal("0")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
        )
        assert v.rows_over_threshold == 1
        # Sentinel divergence value (large positive number).
        assert v.max_divergence_bp > Decimal("1000000")
        # signed_divergence_bp also returns 0 sentinel for divide-by-zero
        # so the operator's reader doesn't blow up.
        assert v.worst_rows[0].signed_divergence_bp == Decimal("0")

    def test_empty_intersection_zero_max(self) -> None:
        lean = {"2026-01-01": Decimal("85.50")}
        cross = {"2025-12-31": Decimal("85.50")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
        )
        assert v.shared_dates == 0
        assert v.max_divergence_bp == Decimal("0")
        assert v.rows_over_threshold == 0
        assert v.passed

    def test_show_worst_zero_returns_empty_tuple(self) -> None:
        lean = {"2026-01-01": Decimal("85.50")}
        cross = {"2026-01-01": Decimal("85.50")}
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("1.0"),
            show_worst=0,
        )
        assert v.worst_rows == ()

    def test_worst_rows_sorted_descending(self) -> None:
        """worst_rows is sorted by divergence_bp descending so the operator
        sees the most-divergent rows first."""
        lean = {
            "2026-01-01": Decimal("85.500"),
            "2026-01-02": Decimal("85.700"),
            "2026-01-03": Decimal("85.500"),
        }
        cross = {
            "2026-01-01": Decimal("85.000"),  # ~58.8bp diff
            "2026-01-02": Decimal("85.690"),  # ~1.17bp diff
            "2026-01-03": Decimal("85.495"),  # ~0.585bp diff
        }
        v = compute_diff(
            ticker="TLT",
            lean_data=lean,
            cross_data=cross,
            threshold_bp=Decimal("100.0"),
            show_worst=10,
        )
        assert [r.date_iso for r in v.worst_rows] == [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
        ]


class TestTickerDiffRow:
    def test_signed_divergence_uses_signed_subtraction(self) -> None:
        r = TickerDiffRow(
            date_iso="2026-01-01",
            lean_close=Decimal("85.60"),
            cross_close=Decimal("85.50"),
            divergence_bp=Decimal("11.7"),
        )
        # (85.60 - 85.50) / 85.50 * 10000 ≈ 11.69590..
        assert r.signed_divergence_bp > Decimal("11.6")
        assert r.signed_divergence_bp < Decimal("11.8")


# ---------------------------------------------------------------------------
# verify_one — happy path + error propagation
# ---------------------------------------------------------------------------


class TestVerifyOne:
    def test_happy_path_passes(self, tmp_path: Path) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})

        def fake_http_get(_url: str) -> str:
            return (
                "Date,Open,High,Low,Close,Volume\n"
                "2026-01-01,85.50,85.65,85.45,85.50,10000000\n"
                "2026-01-02,85.55,85.65,85.49,85.60,11000000\n"
                "2026-01-03,85.59,85.72,85.56,85.70,12000000\n"
            )

        v = verify_one(
            ticker="TLT",
            data_dir=base,
            source="stooq",
            tiingo_api_key=None,
            explicit_start=None,
            explicit_end=None,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
            http_get=fake_http_get,
        )
        assert v.passed
        assert v.fetch_error is None
        assert v.lean_rows == 3
        assert v.cross_rows == 3
        assert v.shared_dates == 3

    def test_missing_zip_returns_fetch_error(self, tmp_path: Path) -> None:
        # tmp_path/equity/usa/daily/ does NOT exist
        v = verify_one(
            ticker="TLT",
            data_dir=tmp_path,
            source="stooq",
            tiingo_api_key=None,
            explicit_start=None,
            explicit_end=None,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
            http_get=lambda _u: "",
        )
        assert v.fetch_error is not None
        assert "LEAN read failed" in v.fetch_error
        assert "FileNotFoundError" in v.fetch_error
        assert not v.passed

    def test_cross_source_fetch_error_surfaced(self, tmp_path: Path) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})

        def boom(_url: str) -> str:
            raise urllib.error.URLError("simulated network failure")

        v = verify_one(
            ticker="TLT",
            data_dir=base,
            source="stooq",
            tiingo_api_key=None,
            explicit_start=None,
            explicit_end=None,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
            http_get=boom,
        )
        assert v.fetch_error is not None
        assert "cross-source fetch failed" in v.fetch_error
        assert "URLError" in v.fetch_error
        # LEAN was read successfully so lean_rows is populated; cross is empty.
        assert v.lean_rows == 3
        assert v.cross_rows == 0

    def test_no_data_response_surfaces_value_error(self, tmp_path: Path) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})

        v = verify_one(
            ticker="TLT",
            data_dir=base,
            source="stooq",
            tiingo_api_key=None,
            explicit_start=None,
            explicit_end=None,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
            http_get=lambda _u: "No data",
        )
        assert v.fetch_error is not None
        assert "ValueError" in v.fetch_error
        assert "no data" in v.fetch_error.lower()

    def test_window_default_derived_from_lean(self, tmp_path: Path) -> None:
        """When --start / --end are NOT explicit, the cross-source query
        window must match the LEAN min/max dates."""
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        captured_urls: list[str] = []

        def fake_http_get(url: str) -> str:
            captured_urls.append(url)
            return "Date,Close\n2026-01-01,85.50\n2026-01-02,85.60\n2026-01-03,85.70\n"

        verify_one(
            ticker="TLT",
            data_dir=base,
            source="stooq",
            tiingo_api_key=None,
            explicit_start=None,
            explicit_end=None,
            threshold_bp=Decimal("1.0"),
            show_worst=5,
            http_get=fake_http_get,
        )
        assert captured_urls
        # d1 = 20260101, d2 = 20260103 (LEAN range)
        assert "d1=20260101" in captured_urls[0]
        assert "d2=20260103" in captured_urls[0]


# ---------------------------------------------------------------------------
# main() CLI driver — exit codes, argparse, json/--out
# ---------------------------------------------------------------------------


class _Recorder:
    """Captures HTTP GETs across the multiplexed ticker loop for assertions."""

    def __init__(self, ticker_to_body: dict[str, str]) -> None:
        self.ticker_to_body = ticker_to_body
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        for ticker, body in self.ticker_to_body.items():
            if f"s={ticker.lower()}.us" in url:
                return body
        return "No data"


class TestMain:
    def test_exit_0_when_all_clean(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        recorder = _Recorder(
            {"TLT": ("Date,Close\n2026-01-01,85.50\n2026-01-02,85.60\n2026-01-03,85.70\n")}
        )

        from scripts import verify_seed_data as mod

        monkeypatch.setattr(mod, "_http_get", recorder)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--data-dir",
                    str(base),
                    "--tickers",
                    "TLT",
                    "--source",
                    "stooq",
                ]
            )
        assert rc == 0
        out = buf.getvalue()
        assert "PASS" in out
        assert "TLT" in out

    def test_exit_1_when_breach(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        # All cross closes are $1 lower than LEAN → ~117bp diff at $85.50.
        recorder = _Recorder(
            {"TLT": ("Date,Close\n2026-01-01,84.50\n2026-01-02,84.60\n2026-01-03,84.70\n")}
        )

        from scripts import verify_seed_data as mod

        monkeypatch.setattr(mod, "_http_get", recorder)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--data-dir",
                    str(base),
                    "--tickers",
                    "TLT",
                ]
            )
        assert rc == 1
        assert "FAIL" in buf.getvalue()

    def test_exit_2_when_fetch_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})

        def boom(_url: str) -> str:
            raise urllib.error.URLError("simulated network failure")

        from scripts import verify_seed_data as mod

        monkeypatch.setattr(mod, "_http_get", boom)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--data-dir",
                    str(base),
                    "--tickers",
                    "TLT",
                ]
            )
        assert rc == 2
        assert "FETCH FAILED" in buf.getvalue()

    def test_exit_2_when_data_dir_does_not_exist(self, tmp_path: Path) -> None:
        rc = main(["--data-dir", str(tmp_path / "missing"), "--tickers", "TLT"])
        assert rc == 2

    def test_exit_2_when_tickers_empty(self, tmp_path: Path) -> None:
        rc = main(["--data-dir", str(tmp_path), "--tickers", ""])
        assert rc == 2

    def test_exit_2_when_negative_threshold(self, tmp_path: Path) -> None:
        _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        rc = main(
            [
                "--data-dir",
                str(tmp_path),
                "--tickers",
                "TLT",
                "--threshold-bp",
                "-1",
            ]
        )
        assert rc == 2

    def test_exit_2_when_negative_show_worst(self, tmp_path: Path) -> None:
        _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        rc = main(
            [
                "--data-dir",
                str(tmp_path),
                "--tickers",
                "TLT",
                "--show-worst",
                "-1",
            ]
        )
        assert rc == 2

    def test_json_mode_emits_structured_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        recorder = _Recorder(
            {"TLT": ("Date,Close\n2026-01-01,85.50\n2026-01-02,85.60\n2026-01-03,85.70\n")}
        )

        from scripts import verify_seed_data as mod

        monkeypatch.setattr(mod, "_http_get", recorder)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--data-dir",
                    str(base),
                    "--tickers",
                    "TLT",
                    "--json",
                ]
            )
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert payload["source"] == "stooq"
        assert payload["window_start"] == "2026-01-01"
        assert payload["window_end"] == "2026-01-03"
        assert payload["threshold_bp"] == "1.0"
        assert len(payload["tickers"]) == 1
        tlt = payload["tickers"][0]
        assert tlt["ticker"] == "TLT"
        assert tlt["passed"] is True
        assert tlt["shared_dates"] == 3
        assert tlt["max_divergence_bp"] == "0"

    def test_out_writes_json_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        recorder = _Recorder(
            {"TLT": ("Date,Close\n2026-01-01,85.50\n2026-01-02,85.60\n2026-01-03,85.70\n")}
        )

        from scripts import verify_seed_data as mod

        monkeypatch.setattr(mod, "_http_get", recorder)
        out_path = tmp_path / "verify.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--data-dir",
                    str(base),
                    "--tickers",
                    "TLT",
                    "--out",
                    str(out_path),
                ]
            )
        assert rc == 0
        assert out_path.is_file()
        payload = json.loads(out_path.read_text())
        assert payload["tickers"][0]["ticker"] == "TLT"

    def test_invalid_source_arg_systemexit(self, tmp_path: Path) -> None:
        """argparse choices reject unknown source values with SystemExit(2)."""
        _build_lean_data_dir(tmp_path, {"TLT": TLT_LEAN_ROWS})
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--data-dir",
                    str(tmp_path),
                    "--tickers",
                    "TLT",
                    "--source",
                    "nope",
                ]
            )
        assert excinfo.value.code == 2

    def test_default_tickers_match_phase_1_etf_universe(self) -> None:
        """The default --tickers must be the 4 Phase 1 ETFs from
        ``Docs/decisions-log.md`` 2026-05-12 seed ceremony."""
        assert DEFAULT_TICKERS == ("TLT", "IEF", "SHY", "TIP")

    def test_default_threshold_bp_is_1(self) -> None:
        """Default threshold = 1bp matches the operator's task spec."""
        assert DEFAULT_THRESHOLD_BP == Decimal("1.0")


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_public_dataclasses_are_frozen(self) -> None:
        """frozen=True is required so the verifications tuple is immutable
        once compute_diff returns it."""
        row = TickerDiffRow(
            date_iso="x",
            lean_close=Decimal("1"),
            cross_close=Decimal("1"),
            divergence_bp=Decimal("0"),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            row.date_iso = "y"  # type: ignore[misc]

        verification = TickerVerification(
            ticker="x",
            lean_rows=0,
            cross_rows=0,
            shared_dates=0,
            dates_only_in_lean=0,
            dates_only_in_cross=0,
            max_divergence_bp=Decimal("0"),
            rows_over_threshold=0,
            worst_rows=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            verification.ticker = "y"  # type: ignore[misc]
