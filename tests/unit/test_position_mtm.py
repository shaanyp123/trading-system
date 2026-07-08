"""Unit tests for :mod:`services.api.position_mtm`.

Crypto-pivot C0 (2026-07-08): the LEAN on-disk price source is retired
(bar_sync deleted, delta spec §1), so the module is fallback-only until
the §3.2 Coinbase market-data source lands. Covered:

* :func:`compute_position_mtm` — known market → fallback (current_price=
  avg_cost, unrealized_pnl=0, source="fallback_avg_cost"); unknown
  market → same numbers with source="unknown_market"; ``price_as_of``
  always ``None`` (no fresh bar is ever read on this path).
"""

from __future__ import annotations

from decimal import Decimal

from services.api.position_mtm import PositionMtmResult, compute_position_mtm


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
