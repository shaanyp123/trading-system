"""Unit tests for :mod:`services.api.position_mtm`.

Crypto-pivot §3.9 (2026-07-09): the module is now the mark ladder's pure
math — rung-1 live-mark uPnL + rung-3 avg-cost fallback. Covered:

* :func:`unrealized_pnl` — the Decimal ``(mark - avg_cost) x qty x
  multiplier`` formula the live-WS rung uses (long/short, nano
  multiplier scaling).
* :func:`compute_position_mtm` — known market → fallback (current_price=
  avg_cost, unrealized_pnl=0, source="fallback_avg_cost"); unknown
  market → same numbers with source="unknown_market"; ``price_as_of``
  always ``None`` (no fresh bar is ever read on this path).
"""

from __future__ import annotations

from decimal import Decimal

from services.api.position_mtm import (
    PositionMtmResult,
    compute_position_mtm,
    unrealized_pnl,
)


class TestUnrealizedPnl:
    def test_long_gain_scales_by_nano_multiplier(self) -> None:
        # (61000 - 60000) x 2 x 0.01 = 20.00
        assert unrealized_pnl(Decimal("61000"), Decimal("60000"), 2, Decimal("0.01")) == Decimal(
            "20.00"
        )

    def test_short_gains_when_mark_drops(self) -> None:
        # (3300 - 3400) x (-4) x 0.1 = +40.0
        assert unrealized_pnl(Decimal("3300"), Decimal("3400"), -4, Decimal("0.1")) == Decimal(
            "40.0"
        )

    def test_long_loss_is_negative(self) -> None:
        assert unrealized_pnl(Decimal("59000"), Decimal("60000"), 1, Decimal("0.01")) == Decimal(
            "-10.00"
        )


class TestComputePositionMtm:
    def test_known_crypto_market_falls_back_to_avg_cost(self) -> None:
        result = compute_position_mtm("BTC", 1, Decimal("50000"))
        assert result == PositionMtmResult(
            current_price=Decimal("50000"),
            unrealized_pnl=Decimal("0"),
            source="fallback_avg_cost",
            price_as_of=None,
        )

    def test_second_crypto_market_falls_back_to_avg_cost(self) -> None:
        result = compute_position_mtm("ETH", 100, Decimal("83.50"))
        assert result.current_price == Decimal("83.50")
        assert result.unrealized_pnl == Decimal("0")
        assert result.source == "fallback_avg_cost"
        assert result.price_as_of is None

    def test_short_position_also_falls_back(self) -> None:
        result = compute_position_mtm("ETH", -1, Decimal("2400"))
        assert result.current_price == Decimal("2400")
        assert result.unrealized_pnl == Decimal("0")
        assert result.source == "fallback_avg_cost"

    def test_unknown_market_flagged(self) -> None:
        result = compute_position_mtm("/MUSTANG", 1, Decimal("100"))
        assert result.current_price == Decimal("100")
        assert result.unrealized_pnl == Decimal("0")
        assert result.source == "unknown_market"
        assert result.price_as_of is None
