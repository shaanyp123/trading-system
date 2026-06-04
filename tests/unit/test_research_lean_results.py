"""LEAN result parser → shared BacktestResult (design §4.3 results.py, P2).

Runs against committed LEAN-output fixtures (tests/fixtures/lean_output/) so the
parser is validated with NO Docker/LEAN. Two fixtures exercise the parser's
tolerance to LEAN's version drift: ``line_pascal`` (line-series ``{x,y}`` equity +
PascalCase keys + int enums) and ``candle_camel`` (candlestick ``{x,...,close}`` +
camelCase keys + string enums).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from research.lean.results import find_result_json, parse_lean_result

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "lean_output"
_LINE_PASCAL = _FIXTURES / "line_pascal"
_CANDLE_CAMEL = _FIXTURES / "candle_camel"


def test_find_result_json_skips_side_files() -> None:
    # The result file has an opaque name (9f3a2.json); order-events + config must
    # be skipped, so content-sniffing (not filename) finds the right document.
    found = find_result_json(_LINE_PASCAL)
    assert found.name == "9f3a2.json"


def test_find_result_json_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_result_json(tmp_path / "does_not_exist")


def test_find_result_json_no_result_raises(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{"environment": "backtesting"}', encoding="utf-8")
    (tmp_path / "x-order-events.json").write_text("[]", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="no LEAN result JSON"):
        find_result_json(tmp_path)


def test_parse_line_series_pascal_case() -> None:
    parsed = parse_lean_result(
        _LINE_PASCAL, symbol="TLT", multiplier=1.0, strategy_name="donchian(20,1)"
    )
    r = parsed.result
    assert r.symbol == "TLT"
    assert r.multiplier == 1.0
    assert r.fill == "lean"
    assert r.strategy_name == "donchian(20,1)"
    assert r.dates == (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
        date(2024, 1, 8),
        date(2024, 1, 9),
    )
    assert r.equity_curve.tolist() == [100000.0, 100000.0, 100100.0, 100150.0, 100120.0, 100120.0]
    assert r.starting_cash == 100000.0
    assert r.final_equity == 100120.0
    assert r.pnl == pytest.approx(120.0)
    assert r.total_return == pytest.approx(0.0012)
    # Positions reconstructed from filled orders' cumulative quantity.
    assert r.positions.tolist() == [0, 0, 1, 1, 0, 0]


def test_parse_line_series_trades() -> None:
    parsed = parse_lean_result(
        _LINE_PASCAL, symbol="TLT", multiplier=1.0, strategy_name="donchian(20,1)"
    )
    assert len(parsed.trades) == 1
    trade = parsed.trades[0]
    assert trade.entry_price == Decimal("95.0")
    assert trade.exit_price == Decimal("98.0")
    assert trade.quantity == 1
    assert trade.direction == 1  # Direction 0 (long) → +1
    assert trade.profit_loss == Decimal("120.0")
    assert trade.fees == Decimal("0.0")
    assert trade.entry_time.date() == date(2024, 1, 4)
    assert parsed.statistics["Total Trades"] == "1"


def test_parse_candlestick_camel_case_and_string_enums() -> None:
    parsed = parse_lean_result(
        _CANDLE_CAMEL, symbol="/MES", multiplier=5.0, strategy_name="donchian(20,1)"
    )
    r = parsed.result
    # Equity from the candlestick "close" field (no "y" present).
    assert r.dates == (date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 5), date(2024, 2, 6))
    assert r.equity_curve.tolist() == [50000.0, 50000.0, 50020.0, 50050.0]
    assert r.pnl == pytest.approx(50.0)
    # Short position opened 02-02 (sell), closed 02-05 (buy).
    assert r.positions.tolist() == [0, -1, 0, 0]
    assert len(parsed.trades) == 1
    trade = parsed.trades[0]
    assert trade.direction == -1  # "Short" string → -1
    assert trade.entry_price == Decimal("4500.0")
    assert trade.exit_price == Decimal("4480.0")
    assert trade.profit_loss == Decimal("20.0")
    assert parsed.statistics["Net Profit"] == "0.04%"


def test_starting_cash_override() -> None:
    parsed = parse_lean_result(
        _LINE_PASCAL,
        symbol="TLT",
        multiplier=1.0,
        strategy_name="donchian(20,1)",
        starting_cash=99000.0,
    )
    # Override wins over the first equity point (the driver knows set_cash).
    assert parsed.result.starting_cash == 99000.0
    assert parsed.result.pnl == pytest.approx(100120.0 - 99000.0)
