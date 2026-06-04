"""Golden: V1 reproduction (log-based) + the order/fill parser, on committed fixtures.

V1 emits decisions via HTTP POST and places NO LEAN orders, so its decisions live in
the LEAN LOG (``v1_signals_generated`` / ``v1_signal_rejected``), NOT the result
JSON's orders. The authoritative V1 reproduction golden therefore parses a REAL
captured V1 log (``tests/fixtures/v1_repro_log/``, from an isolated harness run on the
production ``lean/v1_strategy.py``) and cross-checks against a REAL prod ``signals``
oracle snapshot (``tests/fixtures/v1_oracle/``).

The separate order/fill parser (used by reference strategies that DO place LEAN
orders) is covered against a multi-symbol futures order result.

Structural limits this golden pins (design §8 "P2 landed"): the decision-level match
is partial (4/9 strict, 3/4 markets) because (1) backtest V1 has no position feedback
under PaperBrokerage → it re-emits the same breakout each cycle (vs the position-aware
live system); (2) every live signal used a distinct param-hash (params calibrated
mid-window + the ER gate landed 2026-06-02) vs the uniform-param backtest.
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
    # Deterministic on the committed log: 28 emitted decisions (incl. daily re-emits).
    assert len(entries) == 28
    markets = {e.market for e in entries}
    assert markets == {"/MES", "/MNQ", "TLT", "IEF", "SHY"}
    assert all(e.direction == "long" for e in entries)


def test_v1_reproduction_crosscheck_vs_oracle() -> None:
    entries = parse_v1_decisions_from_log(_V1_LOG_DIR)
    oracle = load_oracle(_ORACLE)
    assert len({e.key() for e in oracle}) == 9  # 18 rows → 9 unique decisions

    strict = crosscheck_entries(entries, oracle)
    # Partial (structural): 4 of 9 oracle decisions reproduced exactly (date+market).
    assert len(strict.matched) == 4
    assert (date(2026, 5, 28), "/MES", "long") in strict.matched
    assert (date(2026, 5, 16), "TLT", "long") in strict.matched

    # Market-level (first entry per market) is the fairer view: 3/4 markets agree.
    bt_markets = {(e.market, e.direction) for e in first_entry_per_market(entries)}
    oracle_markets = {
        (e.market, e.direction)
        for e in first_entry_per_market(
            [Entry(o.session_date, o.market, o.direction) for o in oracle]
        )
    }
    matched = bt_markets & oracle_markets
    assert matched == {("/MES", "long"), ("/MNQ", "long"), ("TLT", "long")}
    assert len(matched) / len(oracle_markets) == 0.75


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
