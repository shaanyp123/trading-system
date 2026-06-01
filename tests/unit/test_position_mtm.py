"""Unit tests for :mod:`services.api.position_mtm`.

Coverage matrix:

* :func:`fetch_current_price` — ETF deci-cent → dollars; futures
  multi-member front-month resolution; missing zip; malformed CSV.
* :func:`compute_position_mtm` — happy-path long futures, long ETF,
  short futures (negative qty PnL), unknown market, missing data fallback.

Tests build minimal LEAN-on-disk fixtures in ``tmp_path`` so the helper
exercises real zipfile + path logic without needing the production
``lean_data`` volume.
"""

from __future__ import annotations

import zipfile
from decimal import Decimal
from pathlib import Path

from services.api.position_mtm import (
    _iso_date_from_bar_field,
    compute_position_mtm,
    fetch_current_price,
)

# ---------------------------------------------------------------------------
# On-disk fixture helpers
# ---------------------------------------------------------------------------


def _write_etf_zip(data_root: Path, ticker: str, *, csv_body: str) -> Path:
    """Create ``equity/usa/daily/<lower>.zip`` containing ``<lower>.csv``."""
    zip_path = data_root / "equity" / "usa" / "daily" / f"{ticker.lower()}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{ticker.lower()}.csv", csv_body)
    return zip_path


def _write_futures_zip(
    data_root: Path,
    ibkr_symbol: str,
    market_dir: str,
    members: dict[str, str],
) -> Path:
    """Create ``future/<market_dir>/daily/<lower>_trade.zip`` with members.

    ``members`` keys are full member names (e.g., ``m2k_trade_202606.csv``).
    """
    zip_path = data_root / "future" / market_dir / "daily" / f"{ibkr_symbol.lower()}_trade.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for member_name, body in members.items():
            zf.writestr(member_name, body)
    return zip_path


# ---------------------------------------------------------------------------
# fetch_current_price tests
# ---------------------------------------------------------------------------


class TestFetchCurrentPriceEtf:
    def test_etf_deci_cents_decoded_to_dollars(self, tmp_path: Path) -> None:
        """ETF CSV has prices in deci-cents (scaled by 10000). Expect
        /10000 in the returned Decimal — quantized to 0.0001."""
        # TLT mocked bars; last close = 836400 deci-cents = $83.64
        csv_body = (
            "20260520 00:00,837000,838500,835000,837200,1000000\n"
            "20260521 00:00,837500,839800,836700,838900,1100000\n"
            "20260527 00:00,838000,840100,835100,836400,1234567\n"
        )
        _write_etf_zip(tmp_path, "TLT", csv_body=csv_body)

        result = fetch_current_price("TLT", tmp_path)
        assert result == Decimal("83.6400")

    def test_etf_missing_zip_returns_none(self, tmp_path: Path) -> None:
        result = fetch_current_price("TLT", tmp_path)
        assert result is None


class TestFetchCurrentPriceFutures:
    def test_futures_picks_member_with_latest_session_date(self, tmp_path: Path) -> None:
        """/M2K has multiple per-contract-month CSVs; the helper picks the
        member whose last bar has the largest session_date (the current
        front month)."""
        # Older CSVs end on earlier dates; current front-month ends 20260527
        members = {
            "m2k_trade_202506.csv": (
                "20250520 00:00,2200,2210,2190,2205,50000\n"
                "20250619 00:00,2210,2222,2200,2215,52000\n"
            ),
            "m2k_trade_202509.csv": (
                "20250622 00:00,2300,2310,2290,2305,40000\n"
                "20250918 00:00,2305,2320,2298,2315,42000\n"
            ),
            "m2k_trade_202606.csv": (
                "20260526 00:00,2887.8,2928.3,2885.1,2925,118070\n"
                "20260527 00:00,2926.5,2933.5,2920.8,2931.2,4075\n"
            ),
        }
        _write_futures_zip(tmp_path, "M2K", "cme", members)

        result = fetch_current_price("/M2K", tmp_path)
        assert result == Decimal("2931.2000")

    def test_futures_missing_zip_returns_none(self, tmp_path: Path) -> None:
        result = fetch_current_price("/M2K", tmp_path)
        assert result is None

    def test_futures_empty_zip_returns_none(self, tmp_path: Path) -> None:
        """Zip exists but has no member matching the futures naming."""
        zip_path = tmp_path / "future" / "cme" / "daily" / "m2k_trade.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("unrelated.txt", "garbage")

        result = fetch_current_price("/M2K", tmp_path)
        assert result is None


class TestFetchCurrentPriceMarketDir:
    def test_mym_routes_via_cbot_market_dir(self, tmp_path: Path) -> None:
        """/MYM lives under future/cbot/ per the 2026-05-23 routing fix
        (PR #226). Verify the helper picks the right market_dir."""
        members = {
            "mym_trade_202606.csv": ("20260527 00:00,42000.5,42100.0,41980.0,42080.5,8000\n"),
        }
        _write_futures_zip(tmp_path, "MYM", "cbot", members)

        result = fetch_current_price("/MYM", tmp_path)
        assert result == Decimal("42080.5000")


# ---------------------------------------------------------------------------
# compute_position_mtm tests
# ---------------------------------------------------------------------------


class TestComputePositionMtm:
    def test_long_futures_in_money(self, tmp_path: Path) -> None:
        """/M2K: 1 LONG contract @ 2925, current 2931.2, multiplier=5.
        unrealized_pnl = (2931.2 - 2925) * 1 * 5 = 31.00"""
        _write_futures_zip(
            tmp_path,
            "M2K",
            "cme",
            {"m2k_trade_202606.csv": "20260527 00:00,2926.5,2933.5,2920.8,2931.2,4075\n"},
        )

        result = compute_position_mtm("/M2K", 1, Decimal("2925"), tmp_path)
        assert result.source == "fresh_bar"
        assert result.current_price == Decimal("2931.2000")
        assert result.unrealized_pnl == Decimal("31.00")

    def test_short_futures_in_money(self, tmp_path: Path) -> None:
        """/MGC SHORT: -1 contract @ 2400, current 2380, multiplier=10.
        Short in-the-money when price drops. PnL math via signed qty:
        (2380 - 2400) * -1 * 10 = +200.00 (gain)"""
        _write_futures_zip(
            tmp_path,
            "MGC",
            "comex",
            {"mgc_trade_202606.csv": "20260527 00:00,2390,2395,2375,2380,3000\n"},
        )

        result = compute_position_mtm("/MGC", -1, Decimal("2400"), tmp_path)
        assert result.source == "fresh_bar"
        assert result.current_price == Decimal("2380.0000")
        assert result.unrealized_pnl == Decimal("200.00")

    def test_long_etf_in_money(self, tmp_path: Path) -> None:
        """TLT: 100 LONG shares @ 83.50, current 83.64, multiplier=1.
        unrealized_pnl = (83.64 - 83.50) * 100 * 1 = 14.00"""
        _write_etf_zip(
            tmp_path,
            "TLT",
            csv_body="20260527 00:00,838000,840100,835100,836400,1000000\n",
        )

        result = compute_position_mtm("TLT", 100, Decimal("83.50"), tmp_path)
        assert result.source == "fresh_bar"
        # 836400 / 10000 = 83.64
        assert result.current_price == Decimal("83.6400")
        # (83.64 - 83.50) * 100 = 14.00
        assert result.unrealized_pnl == Decimal("14.00")

    def test_missing_data_falls_back_to_avg_cost(self, tmp_path: Path) -> None:
        """No zip on disk → returns avg_cost with unrealized_pnl=0."""
        result = compute_position_mtm("/M2K", 1, Decimal("2925"), tmp_path)
        assert result.source == "fallback_avg_cost"
        assert result.current_price == Decimal("2925")
        assert result.unrealized_pnl == Decimal("0")

    def test_unknown_market_falls_back_with_unknown_source(self, tmp_path: Path) -> None:
        """Market outside V1_EXPOSURE_METADATA → unknown_market path."""
        result = compute_position_mtm("/MUSTANG", 1, Decimal("100"), tmp_path)
        assert result.source == "unknown_market"
        assert result.current_price == Decimal("100")
        assert result.unrealized_pnl == Decimal("0")


class TestPriceAsOf:
    """``price_as_of`` = the latest bar's session date (data-freshness feature)."""

    def test_iso_date_from_bar_field(self) -> None:
        assert _iso_date_from_bar_field("20260527 00:00") == "2026-05-27"
        assert _iso_date_from_bar_field("20260601") == "2026-06-01"
        assert _iso_date_from_bar_field("garbage") is None
        assert _iso_date_from_bar_field("") is None

    def test_etf_price_as_of_is_last_bar_date(self, tmp_path: Path) -> None:
        csv_body = (
            "20260520 00:00,837000,838500,835000,837200,1000000\n"
            "20260527 00:00,838000,840100,835100,836400,1234567\n"
        )
        _write_etf_zip(tmp_path, "TLT", csv_body=csv_body)
        result = compute_position_mtm("TLT", 1, Decimal("90"), tmp_path)
        assert result.source == "fresh_bar"
        assert result.price_as_of == "2026-05-27"  # last bar, not the earlier one

    def test_fallback_price_as_of_is_none(self, tmp_path: Path) -> None:
        # No zip on disk → fallback to avg_cost, no bar date to report.
        result = compute_position_mtm("TLT", 1, Decimal("90"), tmp_path)
        assert result.source == "fallback_avg_cost"
        assert result.price_as_of is None
