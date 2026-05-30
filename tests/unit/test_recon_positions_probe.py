"""Unit tests for ``scripts/operator_tools/recon_positions_probe.py``.

A22 N/A: the probe does no DB writes. Tests exercise the argparse surface +
validation rules + the pure ib-async normalization helpers (market mapping,
zero-qty drop, Decimal coercion). The async broker round-trip is exercised by
the operator against the live gateway per the module-docstring runbook (A27).

Coverage matrix:

* Argparse happy path (paper, defaults).
* --client-id out of the 80-99 operator range rejected.
* --client-id at both range boundaries accepted (80, 99).
* --env live-* without --allow-non-paper rejected; with it accepted.
* Negative --settle-delay-seconds rejected.
* --expect-markets parsed to a normalized tuple (whitespace + empties dropped).
* _market_from_ib_contract: FUT → /SYM; STK → SYM.
* _rows_from_positions: zero-qty rows dropped; sorted by market; Decimal coercion.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from scripts.operator_tools.recon_positions_probe import (
    DEFAULT_PROBE_CLIENT_ID,
    DEFAULT_SETTLE_DELAY_SECONDS,
    ParsedArgs,
    ProbePositionRow,
    _market_from_ib_contract,
    _rows_from_positions,
    parse_args,
)

# --------------------------------------------------------------------------
# Fakes mirroring the ib-async shapes the helpers read (duck-typed).
# --------------------------------------------------------------------------


@dataclass
class _FakeContract:
    symbol: str
    secType: str


@dataclass
class _FakePosition:
    contract: _FakeContract
    position: float
    avgCost: float


class TestParseArgs:
    def test_happy_path_paper_defaults(self) -> None:
        result = parse_args(["--env", "paper"])
        assert isinstance(result, ParsedArgs)
        assert result.client_id == DEFAULT_PROBE_CLIENT_ID
        assert result.env == "paper"
        assert result.ibkr_port == 4004
        assert result.account_id is None
        assert result.settle_delay_seconds == DEFAULT_SETTLE_DELAY_SECONDS
        assert result.expect_markets == ()
        assert result.allow_non_paper is False

    def test_client_id_below_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be in 80-99"):
            parse_args(["--env", "paper", "--client-id", "4"])

    def test_client_id_above_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be in 80-99"):
            parse_args(["--env", "paper", "--client-id", "100"])

    @pytest.mark.parametrize("cid", [80, 99])
    def test_client_id_boundaries_accepted(self, cid: int) -> None:
        result = parse_args(["--env", "paper", "--client-id", str(cid)])
        assert result.client_id == cid

    def test_live_env_requires_allow_non_paper(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(["--env", "live-small"])

    def test_live_env_with_allow_non_paper_accepted(self) -> None:
        result = parse_args(["--env", "live-small", "--allow-non-paper", "--ibkr-port", "4003"])
        assert result.env == "live-small"
        assert result.allow_non_paper is True
        assert result.ibkr_port == 4003

    def test_negative_settle_delay_rejected(self) -> None:
        with pytest.raises(ValueError, match="settle-delay-seconds must be >= 0"):
            parse_args(["--env", "paper", "--settle-delay-seconds", "-1"])

    def test_expect_markets_normalized(self) -> None:
        result = parse_args(["--env", "paper", "--expect-markets", " /M2K , /MES ,, "])
        assert result.expect_markets == ("/M2K", "/MES")

    def test_account_id_passthrough(self) -> None:
        result = parse_args(["--env", "paper", "--account-id", "U25655583"])
        assert result.account_id == "U25655583"


class TestMarketNormalization:
    def test_future_gets_slash_prefix(self) -> None:
        assert _market_from_ib_contract(_FakeContract("MES", "FUT")) == "/MES"

    def test_equity_stays_bare(self) -> None:
        assert _market_from_ib_contract(_FakeContract("TLT", "STK")) == "TLT"

    def test_missing_attrs_safe(self) -> None:
        assert _market_from_ib_contract(object()) == ""


class TestRowsFromPositions:
    def test_zero_qty_dropped_and_sorted(self) -> None:
        positions = [
            _FakePosition(_FakeContract("MES", "FUT"), 1.0, 7587.75),
            _FakePosition(_FakeContract("TLT", "STK"), 0.0, 95.0),  # dropped
            _FakePosition(_FakeContract("M2K", "FUT"), -2.0, 2925.0),
        ]
        rows = _rows_from_positions(positions)
        assert rows == (
            ProbePositionRow(market="/M2K", quantity=Decimal("-2"), avg_cost=Decimal("2925")),
            ProbePositionRow(market="/MES", quantity=Decimal("1"), avg_cost=Decimal("7587.75")),
        )

    def test_decimal_coercion_avoids_float_noise(self) -> None:
        rows = _rows_from_positions([_FakePosition(_FakeContract("MGC", "FUT"), 1.0, 0.1)])
        # str()-first coercion keeps the literal, not 0.1000000000000000055…
        assert rows[0].avg_cost == Decimal("0.1")

    def test_empty_input_empty_output(self) -> None:
        assert _rows_from_positions([]) == ()
