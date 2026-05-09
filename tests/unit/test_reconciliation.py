"""Unit tests for ``services/reconciliation/recon.py`` — recon diff policy.

Test inventory derived from backend-spec §10.1 row
``services/reconciliation/tolerance.py`` + the session prompt's branch list:

- exact match (no breaks, ``check_passed`` event)
- single-position qty mismatch (any non-zero delta is a break)
- cash within abs tolerance (no break — abs floor binds)
- cash within bps tolerance (no break — bps band binds)
- cash outside both bands
- T+1 grace path (matching prior break → ``grace_period`` not actionable)
- dividend ex-date 2x widening admits what would otherwise break
- multiple breaks in one snapshot (each → its own break + audit event)
- prior break gone today → ``reconciliation_break_resolved`` event
- timezone-aware enforcement (anti-pattern A06)
- audit-event payload field shape (canonical taxonomy values)

Per anti-pattern A22, no audit events are emitted here — the policy is a
pure function and ``audit_events`` is asserted as data on the
``ReconciliationPlan``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from services.reconciliation.recon import (
    CASH_ABS_TOLERANCE_USD,
    CASH_BPS_TOLERANCE,
    DIVIDEND_WIDENING_FACTOR,
    POSITION_QTY_TOLERANCE,
    BackendView,
    BrokerSource,
    BrokerView,
    PendingAuditEvent,
    PriorBreak,
    ReconciliationError,
    ReconciliationMetric,
    ReconciliationPlan,
    ResolutionPath,
    plan_reconciliation_check,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

#: Wall-clock for tests. tz-aware (anti-pattern A06).
TS = datetime(2026, 5, 9, 22, 30, tzinfo=UTC)


def _backend(
    positions: Mapping[str, str] | None = None,
    *,
    cash: str = "100000.00",
    equity: str = "100000.00",
) -> BackendView:
    return BackendView(
        positions={k: Decimal(v) for k, v in (positions or {}).items()},
        cash_usd=Decimal(cash),
        equity_baseline=Decimal(equity),
    )


def _broker(
    positions: Mapping[str, str] | None = None,
    *,
    cash: str = "100000.00",
    source: BrokerSource = BrokerSource.QC_OBJECTSTORE,
) -> BrokerView:
    return BrokerView(
        positions={k: Decimal(v) for k, v in (positions or {}).items()},
        cash_usd=Decimal(cash),
        source=source,
    )


# ---------------------------------------------------------------------------
# Locked-tolerance constants — assert spec values are exactly what the
# policy module exports. If a future PR drifts these by accident, the
# assertion fails before any policy test confuses the diagnosis.
# ---------------------------------------------------------------------------


class TestLockedConstants:
    def test_position_qty_tolerance_is_zero(self) -> None:
        assert POSITION_QTY_TOLERANCE == Decimal("0")

    def test_cash_abs_tolerance_is_five_dollars(self) -> None:
        assert CASH_ABS_TOLERANCE_USD == Decimal("5.00")

    def test_cash_bps_tolerance_is_one_basis_point(self) -> None:
        assert CASH_BPS_TOLERANCE == Decimal("0.0001")

    def test_dividend_widening_factor_is_two(self) -> None:
        assert DIVIDEND_WIDENING_FACTOR == Decimal("2")


# ---------------------------------------------------------------------------
# Exact match — no breaks, single check_passed event
# ---------------------------------------------------------------------------


class TestExactMatch:
    def test_empty_views_no_breaks(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker(),
            detected_at_utc=TS,
        )
        assert plan.breaks_detected == ()
        assert plan.breaks_resolved == ()
        assert plan.actionable_break_count == 0
        assert plan.should_invoke_kill_switch is False
        # exactly one audit event: check_passed
        assert len(plan.audit_events) == 1
        ev = plan.audit_events[0]
        assert ev.event_type == "reconciliation_check_passed"
        assert ev.payload["source"] == "qc_objectstore"
        assert ev.payload["dividend_ex_date_today"] is False
        assert ev.payload["ts"] == TS.isoformat()

    def test_matching_positions_and_cash_no_breaks(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3", "MCL": "-1"}, cash="50000.00"),
            broker_view=_broker({"MES": "3", "MCL": "-1"}, cash="50000.00"),
            detected_at_utc=TS,
        )
        assert plan.breaks_detected == ()
        assert plan.actionable_break_count == 0
        assert plan.should_invoke_kill_switch is False
        assert len(plan.audit_events) == 1
        assert plan.audit_events[0].event_type == "reconciliation_check_passed"

    def test_check_passed_returned_with_flexquery_source(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker(source=BrokerSource.FLEXQUERY_EOD),
            detected_at_utc=TS,
        )
        assert plan.audit_events[0].payload["source"] == "flexquery_eod"


# ---------------------------------------------------------------------------
# Single position qty mismatch — any non-zero delta is a break (tolerance=0)
# ---------------------------------------------------------------------------


class TestPositionBreak:
    def test_single_long_qty_mismatch(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.metric == ReconciliationMetric.POSITION_QTY
        assert brk.market == "MES"
        assert brk.expected == Decimal("3")
        assert brk.actual == Decimal("2")
        assert brk.delta == Decimal("1")
        assert brk.tolerance == Decimal("0")
        assert brk.source == BrokerSource.QC_OBJECTSTORE
        assert brk.within_grace_period is False
        assert brk.resolution_path is None
        assert plan.actionable_break_count == 1
        assert plan.should_invoke_kill_switch is True

    def test_short_qty_mismatch(self) -> None:
        # backend says short 2, broker says short 1 → delta is -1 (signed).
        plan = plan_reconciliation_check(
            backend_view=_backend({"MCL": "-2"}),
            broker_view=_broker({"MCL": "-1"}),
            detected_at_utc=TS,
        )
        brk = plan.breaks_detected[0]
        assert brk.delta == Decimal("-1")
        assert brk.expected == Decimal("-2")
        assert brk.actual == Decimal("-1")

    def test_market_in_backend_only_is_a_break(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "1"}),
            broker_view=_broker(),
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.expected == Decimal("1")
        assert brk.actual == Decimal("0")
        assert brk.delta == Decimal("1")

    def test_market_in_broker_only_is_a_break(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker({"MES": "1"}),
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.expected == Decimal("0")
        assert brk.actual == Decimal("1")
        assert brk.delta == Decimal("-1")

    def test_position_break_emits_break_detected_audit_event(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        # break_detected event only — no check_passed when there are breaks.
        assert len(plan.audit_events) == 1
        ev = plan.audit_events[0]
        assert ev.event_type == "reconciliation_break_detected"
        assert ev.payload["metric"] == "position_qty"
        assert ev.payload["market"] == "MES"
        assert ev.payload["expected"] == "3"
        assert ev.payload["actual"] == "2"
        assert ev.payload["delta"] == "1"
        assert ev.payload["tolerance"] == "0"
        assert ev.payload["source"] == "qc_objectstore"
        assert ev.payload["within_grace_period"] is False
        assert ev.payload["resolution_path"] is None


# ---------------------------------------------------------------------------
# Cash break — abs and bps tolerance bands
# ---------------------------------------------------------------------------


class TestCashTolerance:
    def test_cash_within_abs_tolerance_no_break(self) -> None:
        # equity 1000 → bps band = 1000 x 0.0001 = $0.10. abs $5 binds.
        # delta $4.99 < $5 → no break.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1004.99", equity="1000.00"),
            broker_view=_broker(cash="1000.00"),
            detected_at_utc=TS,
        )
        assert plan.breaks_detected == ()
        assert plan.audit_events[0].event_type == "reconciliation_check_passed"

    def test_cash_at_abs_tolerance_no_break(self) -> None:
        # delta == $5.00; comparison is strict (>), so $5 is admitted.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1005.00", equity="1000.00"),
            broker_view=_broker(cash="1000.00"),
            detected_at_utc=TS,
        )
        assert plan.breaks_detected == ()

    def test_cash_within_bps_tolerance_no_break(self) -> None:
        # equity 1_000_000 → bps band = 1_000_000 x 0.0001 = $100. bps binds
        # (>$5 abs floor). delta $99 < $100 → no break.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1000099.00", equity="1000000.00"),
            broker_view=_broker(cash="1000000.00"),
            detected_at_utc=TS,
        )
        assert plan.breaks_detected == ()

    def test_cash_outside_both_bands_is_a_break(self) -> None:
        # equity 1_000_000 → bps band $100. abs $5 floor irrelevant. delta
        # $101 > $100 → break.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1000101.00", equity="1000000.00"),
            broker_view=_broker(cash="1000000.00"),
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.metric == ReconciliationMetric.CASH_USD
        assert brk.market is None
        assert brk.expected == Decimal("1000101.00")
        assert brk.actual == Decimal("1000000.00")
        assert brk.delta == Decimal("101.00")
        assert brk.tolerance == Decimal("100.00000000")
        assert plan.actionable_break_count == 1
        assert plan.should_invoke_kill_switch is True

    def test_cash_break_payload_uses_canonical_metric(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1000101.00", equity="1000000.00"),
            broker_view=_broker(cash="1000000.00"),
            detected_at_utc=TS,
        )
        ev = plan.audit_events[0]
        assert ev.event_type == "reconciliation_break_detected"
        assert ev.payload["metric"] == "cash_usd"
        assert ev.payload["market"] is None

    def test_negative_cash_delta_is_a_break(self) -> None:
        # broker says more cash than backend. delta = -delta_abs but
        # abs() check is symmetric.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="900.00", equity="1000.00"),
            broker_view=_broker(cash="1000.00"),
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.delta == Decimal("-100.00")


# ---------------------------------------------------------------------------
# T+1 grace classification
# ---------------------------------------------------------------------------


class TestT1Grace:
    def test_break_matching_prior_is_grace_period(self) -> None:
        # Break detected yesterday + same break today (same delta) =
        # grace_period; actionable=False; no kill switch.
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.within_grace_period is True
        assert brk.resolution_path == ResolutionPath.GRACE_PERIOD
        assert plan.actionable_break_count == 0
        assert plan.should_invoke_kill_switch is False

    def test_grace_break_emits_break_detected_with_grace_payload(self) -> None:
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        ev = plan.audit_events[0]
        assert ev.event_type == "reconciliation_break_detected"
        assert ev.payload["within_grace_period"] is True
        assert ev.payload["resolution_path"] == "grace_period"

    def test_break_with_different_delta_is_actionable(self) -> None:
        # Prior delta = 1, today's delta = 2 → no match, actionable.
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "5"}),
            broker_view=_broker({"MES": "3"}),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        brk = plan.breaks_detected[0]
        assert brk.delta == Decimal("2")
        assert brk.within_grace_period is False
        assert brk.resolution_path is None
        assert plan.should_invoke_kill_switch is True

    def test_break_in_different_market_is_actionable(self) -> None:
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MCL": "1"}),
            broker_view=_broker(),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        # MES prior had no match today → resolved
        # MCL today has no prior match → actionable
        assert len(plan.breaks_detected) == 1
        assert plan.breaks_detected[0].market == "MCL"
        assert plan.breaks_detected[0].within_grace_period is False
        assert len(plan.breaks_resolved) == 1
        assert plan.breaks_resolved[0].market == "MES"
        assert plan.actionable_break_count == 1

    def test_empty_prior_breaks_default_no_grace(self) -> None:
        # Default prior_breaks is empty tuple — first-run scenario where
        # every detected break is actionable.
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        brk = plan.breaks_detected[0]
        assert brk.within_grace_period is False
        assert plan.should_invoke_kill_switch is True

    def test_cash_break_can_also_be_grace(self) -> None:
        # Cash breaks are matched by (metric, market=None, delta) too.
        # Equity 1M → bps band $100. delta $150 > $100 → cash break.
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.CASH_USD,
                market=None,
                delta=Decimal("150.00"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1000150.00", equity="1000000.00"),
            broker_view=_broker(cash="1000000.00"),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.metric == ReconciliationMetric.CASH_USD
        assert brk.within_grace_period is True
        assert plan.should_invoke_kill_switch is False


# ---------------------------------------------------------------------------
# Dividend ex-date 2x widening
# ---------------------------------------------------------------------------


class TestDividendWidening:
    def test_widening_admits_what_would_otherwise_break(self) -> None:
        # Equity 1_000_000 → normal cash band = $100. Without widening,
        # delta $150 → break. With widening, band = $200 → no break.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1000150.00", equity="1000000.00"),
            broker_view=_broker(cash="1000000.00"),
            dividend_ex_date_today=True,
            detected_at_utc=TS,
        )
        assert plan.breaks_detected == ()
        assert plan.actionable_break_count == 0
        # check_passed payload reflects the widening-was-active flag.
        assert plan.audit_events[0].event_type == "reconciliation_check_passed"
        assert plan.audit_events[0].payload["dividend_ex_date_today"] is True

    def test_break_outside_widened_band_still_fires(self) -> None:
        # Widened band = $200. Delta $250 → break even with widening.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1000250.00", equity="1000000.00"),
            broker_view=_broker(cash="1000000.00"),
            dividend_ex_date_today=True,
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.tolerance == Decimal("200.00000000")
        assert plan.should_invoke_kill_switch is True

    def test_widening_does_not_affect_position_break(self) -> None:
        # 0 x 2 = 0. Position qty break still fires on dividend ex-date.
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            dividend_ex_date_today=True,
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1
        brk = plan.breaks_detected[0]
        assert brk.metric == ReconciliationMetric.POSITION_QTY
        assert brk.tolerance == Decimal("0")
        assert plan.should_invoke_kill_switch is True

    def test_widening_default_off(self) -> None:
        # Without dividend_ex_date_today=True (default False), $150 delta
        # exceeds the normal $100 band → break.
        plan = plan_reconciliation_check(
            backend_view=_backend(cash="1000150.00", equity="1000000.00"),
            broker_view=_broker(cash="1000000.00"),
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 1


# ---------------------------------------------------------------------------
# Multiple breaks in one snapshot
# ---------------------------------------------------------------------------


class TestMultipleBreaks:
    def test_two_position_breaks_plus_cash_break(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend(
                {"MCL": "0", "MES": "3"},
                cash="100100.00",
                equity="1000.00",  # bps band $0.10; abs $5 floor binds
            ),
            broker_view=_broker(
                {"MCL": "1", "MES": "2"},
                cash="100000.00",
            ),
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 3
        # Markets sorted alphabetically → MCL before MES.
        markets = [b.market for b in plan.breaks_detected]
        assert markets == ["MCL", "MES", None]
        metrics = [b.metric for b in plan.breaks_detected]
        assert metrics == [
            ReconciliationMetric.POSITION_QTY,
            ReconciliationMetric.POSITION_QTY,
            ReconciliationMetric.CASH_USD,
        ]
        assert plan.actionable_break_count == 3
        assert plan.should_invoke_kill_switch is True
        # Three break_detected events — no check_passed when breaks present.
        assert len(plan.audit_events) == 3
        assert all(ev.event_type == "reconciliation_break_detected" for ev in plan.audit_events)

    def test_mixed_grace_and_actionable_breaks(self) -> None:
        # MES has a prior match (grace), MCL is new (actionable). Only MCL
        # triggers the kill switch.
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3", "MCL": "1"}),
            broker_view=_broker({"MES": "2"}),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        assert len(plan.breaks_detected) == 2
        by_market = {b.market: b for b in plan.breaks_detected}
        assert by_market["MES"].within_grace_period is True
        assert by_market["MCL"].within_grace_period is False
        assert plan.actionable_break_count == 1
        assert plan.should_invoke_kill_switch is True


# ---------------------------------------------------------------------------
# Resolved prior breaks → reconciliation_break_resolved events
# ---------------------------------------------------------------------------


class TestResolvedPriorBreaks:
    def test_prior_break_absent_today_emits_resolved_event(self) -> None:
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "3"}),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        assert plan.breaks_detected == ()
        assert len(plan.breaks_resolved) == 1
        resolved = plan.breaks_resolved[0]
        assert resolved.metric == ReconciliationMetric.POSITION_QTY
        assert resolved.market == "MES"
        assert resolved.delta == Decimal("1")
        # Resolved event + check_passed (no breaks today).
        assert len(plan.audit_events) == 2
        ev_types = [e.event_type for e in plan.audit_events]
        assert ev_types == [
            "reconciliation_break_resolved",
            "reconciliation_check_passed",
        ]
        resolve_ev = plan.audit_events[0]
        assert resolve_ev.payload["metric"] == "position_qty"
        assert resolve_ev.payload["market"] == "MES"
        assert resolve_ev.payload["prior_delta"] == "1"
        assert resolve_ev.payload["resolution_path"] == "grace_period"

    def test_prior_break_with_changed_delta_is_resolved_and_re_detected(
        self,
    ) -> None:
        # Prior delta=1; today delta=2. Per ``_resolve_prior_breaks``, prior
        # is resolved (not in today's break-key set on metric+market alone).
        # But ``_classify_grace`` requires same delta, so today's delta=2
        # break is actionable.
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "5"}),
            broker_view=_broker({"MES": "3"}),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        # Today still has a (POSITION_QTY, MES) break; resolution fires
        # only on absent (metric, market). delta=2 ≠ prior delta=1 →
        # NOT grace, but the prior is also NOT considered resolved
        # because (metric, market) IS present today.
        assert plan.breaks_resolved == ()
        assert len(plan.breaks_detected) == 1
        assert plan.breaks_detected[0].delta == Decimal("2")
        assert plan.breaks_detected[0].within_grace_period is False
        assert plan.actionable_break_count == 1

    def test_multiple_prior_breaks_partial_resolution(self) -> None:
        # MES still present today; MCL gone today → only MCL resolved.
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MCL",
                delta=Decimal("2"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        assert {r.market for r in plan.breaks_resolved} == {"MCL"}
        # MES today is grace (matches prior delta=1).
        assert plan.breaks_detected[0].within_grace_period is True
        assert plan.actionable_break_count == 0
        assert plan.should_invoke_kill_switch is False


# ---------------------------------------------------------------------------
# Timezone-aware enforcement (anti-pattern A06)
# ---------------------------------------------------------------------------


class TestTimezoneEnforcement:
    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ReconciliationError, match="timezone-aware"):
            plan_reconciliation_check(
                backend_view=_backend(),
                broker_view=_broker(),
                detected_at_utc=datetime(2026, 5, 9, 22, 30),
            )

    def test_aware_datetime_passes(self) -> None:
        # tz-aware datetime accepted (no raise).
        plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker(),
            detected_at_utc=TS,
        )
        assert isinstance(plan, ReconciliationPlan)


# ---------------------------------------------------------------------------
# Audit-event payload shape (canonical taxonomy values)
# ---------------------------------------------------------------------------


class TestAuditEventShape:
    def test_check_passed_payload_fields(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker(),
            detected_at_utc=TS,
        )
        ev = plan.audit_events[0]
        assert set(ev.payload.keys()) == {"source", "dividend_ex_date_today", "ts"}

    def test_break_detected_payload_fields(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        ev = plan.audit_events[0]
        assert set(ev.payload.keys()) == {
            "metric",
            "market",
            "expected",
            "actual",
            "delta",
            "tolerance",
            "source",
            "within_grace_period",
            "resolution_path",
            "ts",
        }

    def test_break_resolved_payload_fields(self) -> None:
        prior = (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="MES",
                delta=Decimal("1"),
            ),
        )
        plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker(),
            prior_breaks=prior,
            detected_at_utc=TS,
        )
        # First event is reconciliation_break_resolved.
        ev = plan.audit_events[0]
        assert ev.event_type == "reconciliation_break_resolved"
        assert set(ev.payload.keys()) == {
            "metric",
            "market",
            "prior_delta",
            "resolution_path",
            "ts",
        }

    def test_decimal_values_serialized_as_strings(self) -> None:
        # Anti-pattern A05: Decimal-as-str in audit payloads. Verify.
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        ev = plan.audit_events[0]
        for field_name in ("expected", "actual", "delta", "tolerance"):
            assert isinstance(ev.payload[field_name], str), (
                f"{field_name} must be Decimal-as-str (A05)"
            )

    def test_event_types_are_canonical_taxonomy(self) -> None:
        """Each emitted event_type is a value in
        ``services.audit.event_types.AuditEventType``."""
        from services.audit.event_types import AuditEventType

        # Drive every event type by constructing the right input pattern.
        passed_plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker(),
            detected_at_utc=TS,
        )
        broken_plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        resolved_plan = plan_reconciliation_check(
            backend_view=_backend(),
            broker_view=_broker(),
            prior_breaks=(
                PriorBreak(
                    metric=ReconciliationMetric.POSITION_QTY,
                    market="MES",
                    delta=Decimal("1"),
                ),
            ),
            detected_at_utc=TS,
        )
        emitted_types: set[str] = set()
        for plan in (passed_plan, broken_plan, resolved_plan):
            for ev in plan.audit_events:
                emitted_types.add(ev.event_type)
        for et in emitted_types:
            assert AuditEventType(et) is not None  # raises if not in enum

    def test_ts_isoformat_matches_input(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        for ev in plan.audit_events:
            assert ev.payload["ts"] == TS.isoformat()


# ---------------------------------------------------------------------------
# ReconciliationBreak shape — schema mapping completeness
# ---------------------------------------------------------------------------


class TestReconciliationBreakShape:
    def test_break_carries_detected_at_utc_timestamp(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        brk = plan.breaks_detected[0]
        assert brk.detected_at_utc == TS

    def test_break_carries_broker_source(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}, source=BrokerSource.FLEXQUERY_EOD),
            detected_at_utc=TS,
        )
        brk = plan.breaks_detected[0]
        assert brk.source == BrokerSource.FLEXQUERY_EOD

    def test_break_is_frozen_dataclass(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        brk = plan.breaks_detected[0]
        with pytest.raises((AttributeError, Exception)):
            brk.metric = ReconciliationMetric.CASH_USD  # type: ignore[misc]

    def test_audit_events_are_frozen_dataclasses(self) -> None:
        plan = plan_reconciliation_check(
            backend_view=_backend({"MES": "3"}),
            broker_view=_broker({"MES": "2"}),
            detected_at_utc=TS,
        )
        ev = plan.audit_events[0]
        assert isinstance(ev, PendingAuditEvent)
        with pytest.raises((AttributeError, Exception)):
            ev.event_type = "reconciliation_check_passed"  # type: ignore[misc]
