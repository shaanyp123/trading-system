"""Golden: V1 reproduction (log-based) + the order/fill parser, on committed fixtures.

V1 emits decisions via HTTP POST and places NO LEAN orders, so its decisions live in
the LEAN LOG (``v1_signals_generated`` / ``v1_signal_rejected``), NOT the result
JSON's orders. The authoritative V1 reproduction golden therefore parses a REAL
captured V1 log (``tests/fixtures/v1_repro_log/``, from an isolated harness run on the
production ``lean/v1_strategy.py``) and cross-checks against a REAL prod ``signals``
oracle snapshot (``tests/fixtures/v1_oracle/``).

The separate order/fill parser (used by reference strategies that DO place LEAN
orders) is covered against a multi-symbol futures order result.

What this golden pins (charter PR D, measured 2026-06-11 on the real engine over
2026-05-01→2026-06-08; design doc "PR D" subsection has the full attribution):

* market-level match 4/5 (80%, exact 95% CI [0.28, 0.99]) — the one miss (/M2K) is
  the ER-gate REGIME FLIP (live had no ER gate before 2026-06-02; the backtest runs
  current code — the /M2K 2026-05-26 cycle logs ``efficiency_below_threshold``);
* strict decision-level match 4/10 unique oracle decisions (40%, CI [0.12, 0.74]) —
  every miss/extra attributes to a VERIFIED cause: (a) live dormant anti-pyramiding
  re-emission dups pre-#312 (19 oracle rows → 10 unique keys; the backtest correctly
  suppresses — ``position_already_same_direction``), (b) the ER regime flip, (c) bar
  data revised since live decided (bar_sync overwrites daily; map re-synthesis #326)
  → ``no_breakout`` today where live saw a breakout, (d) sizing-to-zero re-emission
  (``v1_backtest_sizing_empty``: the 25%/name cap clips 1 /MNQ contract ≈$43k
  notional to 0 at $100k → stays flat → re-emits while the trend persists);
* prod stamps a DISTINCT parameter_set_hash per signal, so "single-phash window"
  is NOT a usable filter — regime windows are cut by DATE (ER boundary 2026-06-02).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from research.eval.reproduce_v1 import (
    Entry,
    OracleEntry,
    crosscheck_entries,
    entries_from_fills,
    first_entry_per_market,
    load_oracle,
    parse_v1_decisions_from_log,
)
from research.lean.results import load_result_object, parse_filled_orders

pytestmark = pytest.mark.golden

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_V1_LOG_DIR = _FIXTURES / "v1_repro_log"
_ORACLE = _FIXTURES / "v1_oracle" / "paper_signals_entries.json"
_MULTI_SYMBOL_ORDERS = _FIXTURES / "lean_output" / "multi_symbol_futures"


# --------------------------------------------------------------------------- #
# the authoritative V1 reproduction golden (log-based)
# --------------------------------------------------------------------------- #
def test_v1_decisions_parsed_from_real_log() -> None:
    entries = parse_v1_decisions_from_log(_V1_LOG_DIR)
    # Deterministic on the committed log (real-engine run 2026-05-01→2026-06-08,
    # post-#337 position state + post-#339 cost model): 21 emitted decisions.
    assert len(entries) == 21
    markets = {e.market for e in entries}
    assert markets == {"/MES", "/MNQ", "/MYM", "TLT", "IEF", "SHY"}
    assert all(e.direction == "long" for e in entries)


def test_v1_reproduction_crosscheck_vs_oracle() -> None:
    entries = parse_v1_decisions_from_log(_V1_LOG_DIR)
    oracle = load_oracle(_ORACLE)
    assert len({e.key() for e in oracle}) == 10  # 19 rows → 10 unique decisions

    strict = crosscheck_entries(entries, oracle)
    # Measured (PR D): 4 of 10 unique oracle decisions reproduced exactly
    # (date+market+direction); every miss attributes to a verified cause
    # (module docstring). The 4 matches include the two decisions that became
    # real paper positions (/MES 05-28) and the post-ER-regime entry (/MYM 06-04).
    assert len(strict.matched) == 4
    assert (date(2026, 5, 28), "/MES", "long") in strict.matched
    assert (date(2026, 6, 4), "/MYM", "long") in strict.matched
    assert (date(2026, 5, 16), "TLT", "long") in strict.matched
    assert (date(2026, 5, 17), "TLT", "long") in strict.matched

    # The ER-aligned regime window (gate live on both sides) matches 1/1.
    aligned = crosscheck_entries(entries, oracle, window=(date(2026, 6, 2), date(2026, 6, 8)))
    assert aligned.matched == ((date(2026, 6, 4), "/MYM", "long"),)
    assert aligned.match_rate == 1.0

    # Market-level (first entry per market): 4/5 markets agree (80%); the one
    # miss (/M2K) is the ER-gate regime flip — verified in the committed log
    # (the 2026-05-26 /M2K cycle logs efficiency_below_threshold).
    bt_markets = {(e.market, e.direction) for e in first_entry_per_market(entries)}
    oracle_markets = {
        (e.market, e.direction)
        for e in first_entry_per_market(
            [Entry(o.session_date, o.market, o.direction) for o in oracle]
        )
    }
    matched = bt_markets & oracle_markets
    assert matched == {("/MES", "long"), ("/MNQ", "long"), ("/MYM", "long"), ("TLT", "long")}
    assert len(matched) / len(oracle_markets) == 0.8
    assert ("/M2K", "long") not in bt_markets  # the ER regime flip, documented
    log_text = (_V1_LOG_DIR / "V1TrendFollowingAlgorithm-log.txt").read_text(encoding="utf-8")
    assert "session_date=2026-05-26 market=/M2K reason=efficiency_below_threshold" in log_text


def test_oracle_is_all_long() -> None:
    # The live paper oracle (as of capture) is entry/long only — pins the long
    # assumption the log parser relies on.
    oracle = load_oracle(_ORACLE)
    assert oracle
    assert all(o.direction == "long" for o in oracle)


# --------------------------------------------------------------------------- #
# the order/fill parser (for reference strategies that DO place LEAN orders)
# --------------------------------------------------------------------------- #
def test_order_fill_parser_on_multi_symbol_result() -> None:
    fills = parse_filled_orders(load_result_object(_MULTI_SYMBOL_ORDERS))
    # Two futures entries; market normalization re-attaches the leading slash.
    by_market = {f.market for f in fills}
    assert by_market == {"/M2K", "/MES"}
    entries = entries_from_fills(list(fills))
    assert Entry(date(2026, 5, 27), "/M2K", "long") in entries
    assert Entry(date(2026, 5, 28), "/MES", "long") in entries
    assert all(isinstance(e, (Entry, OracleEntry)) for e in entries)
