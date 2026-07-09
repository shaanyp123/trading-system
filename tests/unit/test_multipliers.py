"""Unit tests for services/risk/multipliers.py (dev-guide §5.7 locked cases)."""

from __future__ import annotations

from decimal import Decimal

from services.risk.multipliers import m_combined


class TestMCombined:
    def test_capital_event_plus_convalescent_is_min_not_product(self) -> None:
        assert m_combined(3, True, Decimal("0")) == Decimal("0.5")  # NOT 0.25

    def test_capital_event_expired_convalescent_still_half(self) -> None:
        assert m_combined(6, True, Decimal("0")) == Decimal("0.5")

    def test_no_active_events_floor_one(self) -> None:
        assert m_combined(0, False, Decimal("0")) == Decimal("1.0")

    def test_monthly_dd_alone(self) -> None:
        assert m_combined(0, False, Decimal("-0.12")) == Decimal("0.5")

    def test_monthly_dd_at_threshold_not_active(self) -> None:
        # Strictly-below threshold semantics: exactly -10% does not trip.
        assert m_combined(0, False, Decimal("-0.10")) == Decimal("1.0")

    def test_capital_event_session_zero_inactive(self) -> None:
        assert m_combined(0, False, Decimal("-0.05")) == Decimal("1.0")
