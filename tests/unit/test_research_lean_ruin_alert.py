"""Ruin detection for the ``engine: lean`` path (research.run.lean_ruin_alert).

§6.2 — a liquidation must be impossible to miss. Two independent signals: a LEAN
tagged margin-call order, AND the equity curve crossing <= $0 (LEAN can run the
account negative and halt WITHOUT a tagged order — the case the tag check alone
misses, observed on a real 50x /MES run that wiped equity to -$7k with 0 events).
"""

from __future__ import annotations

from datetime import date

import numpy as np

from research.eval.results import BacktestResult
from research.lean.results import MarginEvent, ParsedLeanResult
from research.run import lean_ruin_alert


def _parsed(equity: list[float], margin_events: tuple[MarginEvent, ...] = ()) -> ParsedLeanResult:
    n = len(equity)
    result = BacktestResult(
        symbol="/MES",
        dates=tuple(date(2024, 1, 1 + i) for i in range(n)),
        equity_curve=np.array(equity, dtype=np.float64),
        positions=np.zeros(n, dtype=np.int64),
        starting_cash=float(equity[0]),
        multiplier=5.0,
        fill="lean",
        strategy_name="t",
    )
    return ParsedLeanResult(
        result=result, trades=(), fills=(), statistics={}, margin_events=margin_events
    )


def test_no_alert_when_healthy() -> None:
    assert lean_ruin_alert("/MES", _parsed([100_000.0, 103_000.0, 106_000.0])) is None


def test_alert_on_equity_wipeout_without_tagged_event() -> None:
    # The case tag-detection misses: equity crosses <= 0, no margin-call order.
    alert = lean_ruin_alert("/MES", _parsed([100_000.0, 40_000.0, -7_000.0]))
    assert alert is not None
    assert "/MES" in alert and "wiped" in alert


def test_alert_on_tagged_margin_event() -> None:
    event = MarginEvent(event_date=date(2024, 6, 24), market="/MES", quantity=-5, tag="Margin Call")
    alert = lean_ruin_alert("/MES", _parsed([100_000.0, 99_000.0], margin_events=(event,)))
    assert alert is not None
    assert "margin-call" in alert and "2024-06-24" in alert


def test_alert_reports_both_signals() -> None:
    event = MarginEvent(event_date=date(2024, 6, 24), market="/MES", quantity=-5, tag="Margin Call")
    alert = lean_ruin_alert("/MES", _parsed([100_000.0, -1.0], margin_events=(event,)))
    assert alert is not None
    assert "margin-call" in alert and "wiped" in alert
