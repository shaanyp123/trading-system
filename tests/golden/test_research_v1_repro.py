"""Golden: reproduce-V1 parse + oracle cross-check on committed fixtures (§6.5).

Parses the representative V1 LEAN-output golden fixture (no Docker/LEAN) and checks
its entry DECISIONS agree with the live paper signals oracle snapshot. This guards
the parser + cross-check against regressions. The OPERATOR re-captures a REAL V1
result + a REAL oracle during P2 acceptance and drops them in (research/lean/README
→ reproduce-V1); the integration test then runs the same assertions against the
real engine.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from research.eval.reproduce_v1 import crosscheck_entries, entries_from_fills, load_oracle
from research.lean.results import load_result_object, parse_filled_orders, parse_lean_result

pytestmark = pytest.mark.golden

_V1_GOLDEN = Path(__file__).parent.parent / "fixtures" / "lean_output" / "v1_repro"
_ORACLE = (
    Path(__file__).parent.parent / "fixtures" / "v1_oracle" / "paper_signals_entries.example.json"
)


def test_v1_golden_parses_to_backtest_result() -> None:
    parsed = parse_lean_result(
        _V1_GOLDEN, symbol="PORTFOLIO", multiplier=1.0, strategy_name="v1_trend_following"
    )
    r = parsed.result
    assert r.dates[0] == date(2026, 5, 26)
    assert r.dates[-1] == date(2026, 6, 2)
    assert r.equity_curve[0] == 100000.0
    assert r.pnl == pytest.approx(210.0)
    # Two entries accumulate to a 2-contract portfolio (M2K 05-27, MES 05-28).
    assert r.positions.tolist() == [0, 1, 2, 2, 2, 2]
    assert parsed.statistics["Total Trades"] == "2"


def test_v1_golden_entries_agree_with_oracle() -> None:
    fills = parse_filled_orders(load_result_object(_V1_GOLDEN))
    entries = entries_from_fills(list(fills))
    oracle = load_oracle(_ORACLE)
    report = crosscheck_entries(entries, oracle)
    assert report.match_rate == 1.0, report.summary()
    assert report.missing_in_backtest == ()
    assert report.extra_in_backtest == ()
    # The two earliest live entries are reproduced at the decision level.
    assert (date(2026, 5, 27), "/M2K", "long") in report.matched
    assert (date(2026, 5, 28), "/MES", "long") in report.matched
