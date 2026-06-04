"""Daily loader reads LEAN on-disk format correctly (design §5.1)."""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from research.data.bars import BarSeries
from research.data.daily_loader import load_daily_series
from tests.unit._research_fixtures import write_equity_daily, write_futures_daily

_ETF_BARS = [
    (date(2024, 1, 2), 99.50, 100.50, 99.00, 100.00, 1_000_000.0),
    (date(2024, 1, 3), 100.00, 101.00, 99.75, 100.75, 1_100_000.0),
    (date(2024, 1, 4), 100.75, 102.00, 100.50, 101.25, 900_000.0),
]
_FUT_BARS = [
    (date(2024, 1, 2), 5000.0, 5010.0, 4990.0, 5005.0, 12000.0),
    (date(2024, 1, 3), 5005.0, 5040.0, 5000.0, 5030.0, 13000.0),
    (date(2024, 1, 4), 5030.0, 5050.0, 5010.0, 5015.0, 11000.0),
]


def test_equity_deci_cent_round_trip(tmp_path: Path) -> None:
    write_equity_daily(tmp_path, "TLT", _ETF_BARS)
    series = load_daily_series(tmp_path, "TLT")
    assert series.dates == (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    # $100.00 round-trips from the deci-cent integer 1_000_000.
    assert series.close.tolist() == [100.00, 100.75, 101.25]
    assert series.open[0] == pytest.approx(99.50)
    assert series.expiry is None


def test_futures_single_expiry_raw_prices(tmp_path: Path) -> None:
    write_futures_daily(tmp_path, "/MES", "202403", _FUT_BARS, market_dir="cme")
    series = load_daily_series(tmp_path, "/MES", expiry="202403")
    assert series.close.tolist() == [5005.0, 5030.0, 5015.0]
    assert series.expiry == "202403"


def test_futures_multi_expiry_requires_disambiguation(tmp_path: Path) -> None:
    write_futures_daily(tmp_path, "/MES", "202403", _FUT_BARS, market_dir="cme")
    write_futures_daily(tmp_path, "/MES", "202406", _FUT_BARS, market_dir="cme")
    with pytest.raises(ValueError, match="pass expiry"):
        load_daily_series(tmp_path, "/MES")
    # ...but selecting one works.
    series = load_daily_series(tmp_path, "/MES", expiry="202406")
    assert len(series) == 3


def test_missing_zip_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_daily_series(tmp_path, "TLT")


def test_missing_expiry_raises(tmp_path: Path) -> None:
    write_futures_daily(tmp_path, "/MES", "202403", _FUT_BARS, market_dir="cme")
    with pytest.raises(KeyError):
        load_daily_series(tmp_path, "/MES", expiry="209912")


def test_inclusive_date_filter(tmp_path: Path) -> None:
    write_equity_daily(tmp_path, "TLT", _ETF_BARS)
    series = load_daily_series(tmp_path, "TLT", start=date(2024, 1, 3), end=date(2024, 1, 3))
    assert series.dates == (date(2024, 1, 3),)
    assert series.close.tolist() == [100.75]


def test_empty_filter_raises(tmp_path: Path) -> None:
    write_equity_daily(tmp_path, "TLT", _ETF_BARS)
    with pytest.raises(ValueError, match="no bars"):
        load_daily_series(tmp_path, "TLT", start=date(2025, 1, 1))


def test_foreign_member_not_loaded_as_wrong_instrument(tmp_path: Path) -> None:
    # A mis-packed zip (a foreign instrument's CSV inside mes_trade.zip) must NOT
    # silently load as /MES — the silent-wrong-history class §5.3 warns about.
    path = tmp_path / "future" / "cme" / "daily" / "mes_trade.zip"
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mnq_trade_202403.csv", "20240102 00:00,1,2,3,4,5\n")
    with pytest.raises(ValueError, match="matching"):
        load_daily_series(tmp_path, "/MES")


def test_empty_bar_series_rejected() -> None:
    empty = np.asarray([], dtype=np.float64)
    with pytest.raises(ValueError, match="at least one bar"):
        BarSeries(
            symbol="X",
            dates=(),
            open=empty,
            high=empty,
            low=empty,
            close=empty,
            volume=empty,
        )
