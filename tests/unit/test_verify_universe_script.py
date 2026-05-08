"""Unit tests for ``scripts/verify_universe.py``.

The script is a thin CLI wrapper around ``services.risk.sizing._stage_0_universe_filter``
(single source of truth for the Stage 0 rule). These tests verify:
- All four equity tiers from the implementation guide (Day 6 11:00 / Day 7
  09:00) produce a valid Stage 0 partition.
- The ``--include-mes-override`` flag correctly activates the /MES override
  at $20k+.
- JSON output is structurally well-formed.

The deeper Stage 0 algorithm is tested in ``tests/unit/test_sizing.py``.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from decimal import Decimal

import pytest

from scripts.verify_universe import V1_NOTIONALS_USD, main
from strategies.v1_trend_following.parameters import V1_CANDIDATE_UNIVERSE


class TestVerifyUniverseScript:
    def test_v1_notionals_covers_every_locked_candidate(self) -> None:
        """The hardcoded notional table must cover every market in
        ``V1_CANDIDATE_UNIVERSE`` — otherwise ``_build_metadata`` raises."""
        for market in V1_CANDIDATE_UNIVERSE:
            assert market in V1_NOTIONALS_USD

    @pytest.mark.parametrize("equity", [15000, 25000, 50000, 100000])
    def test_main_runs_at_each_equity_tier(self, equity: int) -> None:
        """At each Phase 0 equity tier the script exits 0 and prints a table."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--equity", str(equity)])
        assert rc == 0
        out = buf.getvalue()
        assert "Stage 0 verification at equity" in out
        assert "Summary:" in out

    def test_main_json_output_well_formed(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--equity", "50000", "--json"])
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert payload["equity"] == "50000"
        assert payload["threshold_50pct"] == "25000.00"
        assert payload["n_total"] == len(V1_CANDIDATE_UNIVERSE)
        assert isinstance(payload["markets"], list)
        # Every locked market appears in the output.
        market_set = {row["market"] for row in payload["markets"]}
        assert market_set == set(V1_CANDIDATE_UNIVERSE)

    def test_main_mes_override_admits_mes_at_20k(self) -> None:
        """At $20k equity, the override flag promotes /MES into the active set."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--equity", "20000", "--include-mes-override", "--json"])
        assert rc == 0
        payload = json.loads(buf.getvalue())
        mes_row = next(row for row in payload["markets"] if row["market"] == "/MES")
        assert mes_row["override_applied"] is True
        assert mes_row["stage_0_pass"] is True

    def test_main_no_override_excludes_mes_at_20k(self) -> None:
        """Without the flag, /MES at $20k fails Stage 0 (notional $26k > $10k)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--equity", "20000", "--json"])
        assert rc == 0
        payload = json.loads(buf.getvalue())
        mes_row = next(row for row in payload["markets"] if row["market"] == "/MES")
        assert mes_row["override_applied"] is False
        assert mes_row["stage_0_pass"] is False

    def test_main_negative_equity_returns_exit_code_2(self) -> None:
        rc = main(["--equity", "-100"])
        assert rc == 2

    def test_main_15k_pass_count_covers_etf_subset(self) -> None:
        """At $15k all micros should fail Stage 0 (smallest /MCL is $8k > $7.5k);
        only the four ETFs pass. Captures the implementation-guide §3 Week 1
        verification expectation 'at least 4 markets active at $15k'."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--equity", "15000", "--json"])
        payload = json.loads(buf.getvalue())
        passing = [row["market"] for row in payload["markets"] if row["stage_0_pass"]]
        assert set(passing) == {"TLT", "IEF", "SHY", "TIP"}

    def test_main_100k_pass_count_includes_all_candidates(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--equity", "100000", "--json"])
        payload = json.loads(buf.getvalue())
        assert payload["n_pass"] == len(V1_CANDIDATE_UNIVERSE)


def test_threshold_value_formatted_with_two_decimals() -> None:
    """Sanity check on Decimal arithmetic: ``0.50 * 50000`` should serialize
    cleanly. Catches regressions where Decimal context surprises strip
    trailing zeros."""
    threshold = Decimal("0.50") * Decimal("50000")
    assert str(threshold) == "25000.00"
