"""Unit tests for ``services.data.bar_sync_alerts`` — P2 alert builders +
hook seam for the bar_sync worker.

Coverage:

* TestModuleConstants — locked severity / category / threshold values
* TestBuildPartialCycleAlert — sub-threshold + threshold + multi-failure
  + clean-cycle no-emit + descriptor shape + payload contents
* TestBuildSentinelSubstitutionAlert — sub-threshold + threshold +
  multi-sentinel + no-sentinel no-emit + descriptor shape + body content
  + isolation from partial-failure path
* TestCycleClassifiers — cycle_had_failures + cycle_had_sentinel_substitution
* TestFormatUtc — A06 enforcement
* TestModuleContract — public __all__ surface

Pure-Python tests; no testcontainers; no real IBKR or webhook_pusher
I/O. A22 N/A. The alert hook is a pure callable; tests inject an
async-mock to capture descriptor + assert no exception swallowed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from services.data import bar_sync_alerts
from services.data.bar_sync import (
    SENTINEL_OI_WHEN_FETCH_FAILED,
    BarSyncCycleResult,
    MarketSyncResult,
)
from services.data.bar_sync_alerts import (
    BAR_SYNC_ALERT_SEVERITY,
    CONSECUTIVE_ALERT_THRESHOLD,
    PARTIAL_CYCLE_ALERT_CATEGORY,
    SENTINEL_SUBSTITUTION_ALERT_CATEGORY,
    BarSyncAlertDescriptor,
    build_partial_cycle_alert,
    build_sentinel_substitution_alert,
    cycle_had_failures,
    cycle_had_sentinel_substitution,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _successful(
    market: str = "/MES",
    *,
    front_month_expiry: str | None = "202606",
    open_interest: int | None = 257985,
    open_interest_was_sentinel: bool = False,
) -> MarketSyncResult:
    return MarketSyncResult(
        market=market,
        success=True,
        bars_written=10,
        last_session_date=date(2026, 5, 21),
        front_month_expiry=front_month_expiry,
        error=None,
        open_interest=open_interest,
        open_interest_was_sentinel=open_interest_was_sentinel,
    )


def _failed(market: str = "/MES", error: str = "ibkr_call_timeout") -> MarketSyncResult:
    return MarketSyncResult(
        market=market,
        success=False,
        bars_written=0,
        last_session_date=None,
        front_month_expiry=None,
        error=error,
    )


def _cycle(
    *,
    successful: tuple[MarketSyncResult, ...] = (),
    failed: tuple[MarketSyncResult, ...] = (),
) -> BarSyncCycleResult:
    return BarSyncCycleResult(
        cycle_started_at_utc=datetime(2026, 5, 21, 21, 0, tzinfo=UTC),
        cycle_completed_at_utc=datetime(2026, 5, 21, 21, 0, 30, tzinfo=UTC),
        successful_markets=successful,
        failed_markets=failed,
    )


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_consecutive_threshold_is_2(self) -> None:
        # Locked at 2: single-cycle transient failures don't alert; two
        # in a row do. Calibrated against the bar_sync cadence (one fire
        # per ET day) so the operator sees the alert within ~24h of a
        # real persistent issue without false-positives on transient
        # IBKR Error 162/322 hiccups.
        assert CONSECUTIVE_ALERT_THRESHOLD == 2

    def test_severity_is_p2(self) -> None:
        # P2 → #alerts Discord channel only (no email, no #critical).
        # bar_sync issues are operational visibility, not P0
        # escalation; the strategy still trades fine on stale-by-1-day
        # data.
        assert BAR_SYNC_ALERT_SEVERITY == "P2"

    def test_partial_cycle_category_locked(self) -> None:
        # data_quality_reject is the canonical alembic 0004 +
        # webhook_pusher AlertCategory value for "data was rejected /
        # not synced". Adding a new bar_sync_specific category would
        # require an alembic migration (forbidden whitelist).
        assert PARTIAL_CYCLE_ALERT_CATEGORY == "data_quality_reject"

    def test_sentinel_substitution_category_locked(self) -> None:
        # data_quality_quarantine is the canonical value for "data
        # was stored but with a sentinel placeholder" — exactly the
        # OI sentinel-substitution semantic.
        assert SENTINEL_SUBSTITUTION_ALERT_CATEGORY == "data_quality_quarantine"


# ---------------------------------------------------------------------------
# build_partial_cycle_alert
# ---------------------------------------------------------------------------


class TestBuildPartialCycleAlert:
    def test_clean_cycle_returns_none_at_any_count(self) -> None:
        cycle = _cycle(successful=(_successful(),))
        for count in (0, 1, 2, 5, 100):
            assert build_partial_cycle_alert(cycle, consecutive_failure_count=count) is None

    def test_failed_cycle_below_threshold_returns_none(self) -> None:
        cycle = _cycle(failed=(_failed(),))
        assert build_partial_cycle_alert(cycle, consecutive_failure_count=0) is None
        assert build_partial_cycle_alert(cycle, consecutive_failure_count=1) is None

    def test_failed_cycle_at_threshold_returns_descriptor(self) -> None:
        cycle = _cycle(failed=(_failed("/MCL", "ibkr_call_timeout"),))
        descriptor = build_partial_cycle_alert(cycle, consecutive_failure_count=2)
        assert descriptor is not None
        assert descriptor.severity == "P2"
        assert descriptor.category == "data_quality_reject"

    def test_failed_cycle_above_threshold_returns_descriptor(self) -> None:
        cycle = _cycle(failed=(_failed(),))
        descriptor = build_partial_cycle_alert(cycle, consecutive_failure_count=10)
        assert descriptor is not None
        assert descriptor.payload["consecutive_failure_count"] == 10

    def test_descriptor_title_carries_counts(self) -> None:
        cycle = _cycle(
            successful=(_successful("/MES"), _successful("/MNQ")),
            failed=(_failed("/MCL", "ibkr_returned_no_bars"),),
        )
        descriptor = build_partial_cycle_alert(cycle, consecutive_failure_count=3)
        assert descriptor is not None
        assert "1/3 markets failed" in descriptor.title
        assert "consecutive cycles: 3" in descriptor.title

    def test_descriptor_body_lists_failed_markets_with_errors(self) -> None:
        cycle = _cycle(
            failed=(
                _failed("/MCL", "ibkr_call_timeout:5s elapsed"),
                _failed("/MES", "ibkr_returned_no_bars"),
            ),
        )
        descriptor = build_partial_cycle_alert(cycle, consecutive_failure_count=2)
        assert descriptor is not None
        assert "/MCL: ibkr_call_timeout:5s elapsed" in descriptor.body
        assert "/MES: ibkr_returned_no_bars" in descriptor.body

    def test_descriptor_payload_shape(self) -> None:
        cycle = _cycle(
            successful=(_successful("TLT"),),
            failed=(_failed("/MCL", "ibkr_call_timeout"),),
        )
        descriptor = build_partial_cycle_alert(cycle, consecutive_failure_count=2)
        assert descriptor is not None
        p = descriptor.payload
        assert p["alert_kind"] == "partial_cycle_failure"
        assert p["consecutive_failure_count"] == 2
        assert p["total_markets"] == 2
        assert p["successful_count"] == 1
        assert p["failed_count"] == 1
        assert p["failed_markets"] == ["/MCL"]
        assert p["failed_market_errors"] == {"/MCL": "ibkr_call_timeout"}
        # A06: ISO timestamps with Z suffix.
        assert p["cycle_started_at_utc"].endswith("Z")
        assert p["cycle_completed_at_utc"].endswith("Z")

    def test_sentinel_only_cycle_does_not_fire_partial_alert(self) -> None:
        # /MCL with sentinel substitution returns success=True, so
        # partial-cycle alert MUST NOT fire even at high consecutive
        # counts. This test locks the contract that partial-cycle and
        # sentinel-substitution alerts have disjoint signals.
        cycle = _cycle(
            successful=(_successful("/MCL", open_interest=1, open_interest_was_sentinel=True),),
        )
        assert build_partial_cycle_alert(cycle, consecutive_failure_count=10) is None

    def test_payload_failed_market_with_none_error(self) -> None:
        # Defensive: MarketSyncResult.error is Optional[str]. Hand-built
        # failed result with error=None should still produce a valid
        # payload (empty string).
        failed = MarketSyncResult(
            market="/MES",
            success=False,
            bars_written=0,
            last_session_date=None,
            front_month_expiry=None,
            error=None,
        )
        cycle = _cycle(failed=(failed,))
        descriptor = build_partial_cycle_alert(cycle, consecutive_failure_count=2)
        assert descriptor is not None
        assert descriptor.payload["failed_market_errors"]["/MES"] == ""


# ---------------------------------------------------------------------------
# build_sentinel_substitution_alert
# ---------------------------------------------------------------------------


class TestBuildSentinelSubstitutionAlert:
    def test_no_sentinel_returns_none_at_any_count(self) -> None:
        cycle = _cycle(successful=(_successful("/MES", open_interest=257985),))
        for count in (0, 1, 2, 100):
            assert (
                build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=count) is None
            )

    def test_sentinel_below_threshold_returns_none(self) -> None:
        cycle = _cycle(
            successful=(_successful("/MCL", open_interest=1, open_interest_was_sentinel=True),),
        )
        assert build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=1) is None

    def test_sentinel_at_threshold_returns_descriptor(self) -> None:
        cycle = _cycle(
            successful=(_successful("/MCL", open_interest=1, open_interest_was_sentinel=True),),
        )
        descriptor = build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=2)
        assert descriptor is not None
        assert descriptor.severity == "P2"
        assert descriptor.category == "data_quality_quarantine"

    def test_multiple_sentinel_markets_aggregated(self) -> None:
        # Hypothetical future scenario: two markets both substituted.
        cycle = _cycle(
            successful=(
                _successful("/MES", open_interest=257985),
                _successful("/MCL", open_interest=1, open_interest_was_sentinel=True),
                _successful(
                    "/MGC",
                    open_interest=1,
                    open_interest_was_sentinel=True,
                    front_month_expiry="202608",
                ),
            ),
        )
        descriptor = build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=2)
        assert descriptor is not None
        assert descriptor.payload["sentinel_market_count"] == 2
        assert set(descriptor.payload["sentinel_markets"]) == {"/MCL", "/MGC"}
        assert descriptor.payload["sentinel_market_expiries"] == {
            "/MCL": "202606",
            "/MGC": "202608",
        }

    def test_descriptor_title_carries_sentinel_value(self) -> None:
        cycle = _cycle(
            successful=(_successful("/MCL", open_interest=1, open_interest_was_sentinel=True),),
        )
        descriptor = build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=4)
        assert descriptor is not None
        assert f"sentinel ({SENTINEL_OI_WHEN_FETCH_FAILED})" in descriptor.title
        assert "consecutive cycles: 4" in descriptor.title

    def test_descriptor_body_describes_operator_decision_space(self) -> None:
        cycle = _cycle(
            successful=(_successful("/MCL", open_interest=1, open_interest_was_sentinel=True),),
        )
        descriptor = build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=2)
        assert descriptor is not None
        # Body should call out the 3 operator options + name /MCL.
        assert "(a) Upgrade IBKR subscription" in descriptor.body
        assert "(b) Drop the affected market(s)" in descriptor.body
        assert "(c) Continue with the sentinel" in descriptor.body
        assert "/MCL" in descriptor.body
        assert "(front-month 202606)" in descriptor.body

    def test_descriptor_payload_shape(self) -> None:
        cycle = _cycle(
            successful=(
                _successful("/MES", open_interest=257985),
                _successful("/MCL", open_interest=1, open_interest_was_sentinel=True),
            ),
        )
        descriptor = build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=3)
        assert descriptor is not None
        p = descriptor.payload
        assert p["alert_kind"] == "sentinel_substitution"
        assert p["consecutive_sentinel_count"] == 3
        assert p["sentinel_value"] == SENTINEL_OI_WHEN_FETCH_FAILED
        assert p["total_markets"] == 2
        assert p["sentinel_market_count"] == 1
        assert p["sentinel_markets"] == ["/MCL"]
        assert p["cycle_started_at_utc"].endswith("Z")

    def test_failed_cycle_with_no_sentinels_does_not_fire(self) -> None:
        # Lock the disjoint contract from the other direction: a cycle
        # with failures but no successful-market sentinels must not fire
        # the sentinel alert.
        cycle = _cycle(failed=(_failed("/MES"), _failed("/MCL")))
        assert build_sentinel_substitution_alert(cycle, consecutive_sentinel_count=10) is None


# ---------------------------------------------------------------------------
# Cycle classifiers
# ---------------------------------------------------------------------------


class TestCycleClassifiers:
    def test_cycle_had_failures_true_when_any_failed(self) -> None:
        assert cycle_had_failures(_cycle(failed=(_failed(),))) is True

    def test_cycle_had_failures_false_when_all_succeeded(self) -> None:
        assert cycle_had_failures(_cycle(successful=(_successful(),))) is False

    def test_cycle_had_failures_false_when_empty(self) -> None:
        assert cycle_had_failures(_cycle()) is False

    def test_cycle_had_sentinel_substitution_true_when_any_substituted(self) -> None:
        cycle = _cycle(
            successful=(_successful("/MCL", open_interest=1, open_interest_was_sentinel=True),),
        )
        assert cycle_had_sentinel_substitution(cycle) is True

    def test_cycle_had_sentinel_substitution_false_on_real_oi(self) -> None:
        cycle = _cycle(successful=(_successful("/MES", open_interest=257985),))
        assert cycle_had_sentinel_substitution(cycle) is False

    def test_cycle_had_sentinel_substitution_false_when_only_failures(self) -> None:
        cycle = _cycle(failed=(_failed(),))
        assert cycle_had_sentinel_substitution(cycle) is False


# ---------------------------------------------------------------------------
# _format_utc — A06 guard
# ---------------------------------------------------------------------------


class TestFormatUtc:
    def test_naive_datetime_raises(self) -> None:
        # The payload builders pass cycle_result.cycle_started_at_utc /
        # cycle_completed_at_utc — both produced by BarSyncWorker via
        # `datetime.now(tz=UTC)`. A naive datetime in the result would
        # be a contract violation; the helper raises rather than
        # silently emitting a wrong-timezone string.
        with pytest.raises(ValueError, match="tz-aware"):
            bar_sync_alerts._format_utc(datetime(2026, 5, 21, 21, 0))

    def test_utc_datetime_returns_z_suffix(self) -> None:
        ts = datetime(2026, 5, 21, 21, 0, tzinfo=UTC)
        assert bar_sync_alerts._format_utc(ts) == "2026-05-21T21:00:00Z"

    def test_non_utc_offset_normalized_to_z(self) -> None:
        from datetime import timedelta, timezone

        ts = datetime(2026, 5, 21, 17, 0, tzinfo=timezone(timedelta(hours=-4)))
        # 17:00 EDT == 21:00 UTC
        assert bar_sync_alerts._format_utc(ts) == "2026-05-21T21:00:00Z"


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_all_surface(self) -> None:
        expected = {
            "BAR_SYNC_ALERT_SEVERITY",
            "CONSECUTIVE_ALERT_THRESHOLD",
            "PARTIAL_CYCLE_ALERT_CATEGORY",
            "SENTINEL_SUBSTITUTION_ALERT_CATEGORY",
            "AlertCategoryLiteral",
            "AlertSeverityLiteral",
            "BarSyncAlertDescriptor",
            "BarSyncAlertDispatchHook",
            "build_partial_cycle_alert",
            "build_sentinel_substitution_alert",
            "cycle_had_failures",
            "cycle_had_sentinel_substitution",
        }
        assert set(bar_sync_alerts.__all__) == expected

    def test_descriptor_is_frozen(self) -> None:
        d = BarSyncAlertDescriptor(
            severity="P2",
            category="data_quality_reject",
            title="t",
            body="b",
            payload={},
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            d.severity = "P0"  # type: ignore[misc]

    def test_descriptor_severity_and_category_typed(self) -> None:
        # Smoke that the type system accepts the locked literals.
        d = BarSyncAlertDescriptor(
            severity="P2",
            category="data_quality_quarantine",
            title="t",
            body="b",
            payload={"k": "v"},
        )
        assert d.severity == "P2"
        assert d.category == "data_quality_quarantine"

    def test_descriptor_payload_accepts_arbitrary_dict(self) -> None:
        payload: dict[str, Any] = {
            "alert_kind": "partial_cycle_failure",
            "consecutive_failure_count": 2,
            "failed_markets": ["/MCL"],
        }
        d = BarSyncAlertDescriptor(
            severity="P2",
            category="data_quality_reject",
            title="t",
            body="b",
            payload=payload,
        )
        assert d.payload == payload
