"""Unit tests for ``services/calibration/calibration.py`` — slippage-fit policy.

Test inventory derived from backend-spec §2.14 + §3.12 + the IG Week 5 Thu
task brief:

- locked constants (BOOTSTRAP_ALPHA/BETA, MIN_OBSERVATIONS_FOR_OLS, BPS_PER_UNIT)
- enum values mirror §3.12 CHECK constraints + §3.30 audit taxonomy
- empty-fills bootstrap path (zero-prior; empty per_market dict)
- non-empty bootstrap path (zero-prior preserved across all observed markets)
- non-bootstrap with no fills falls back to zero-prior (defensive)
- non-bootstrap with insufficient fills (n < 30) falls back to zero-prior per market
- non-bootstrap with sufficient fills produces real OLS coefficients per market
- per-market mixed counts: one market OLS, another zero-prior
- slippage sign convention: buy above expected → +bps; sell below expected → +bps
- known-fixture OLS values match a hand-computed regression
- zero-variance regressor (Sxx == 0) -> beta=0, alpha=ybar, r_squared=None
- zero-variance response (Syy == 0) with non-zero Sxx -> r_squared=None
- audit-event payload shape (Decimal-as-str, ISO timestamps with tz, fields locked)
- audit-event event_type matches the locked enum value
- exactly one audit event per plan (slippage_calibration_recalibrated)
- timezone enforcement on calibrated_at_utc + each fill_at_utc (A06)
- positive-only ADV / size / prices (A05 + boundary)
- float input rejection on numeric columns (A05)
- non-empty market string required
- direction must be 'buy' or 'sell'
- version_no = parent + 1 (or 1 when None)
- becomes_head flows through to plan + audit event
- data_window matches min/max of fill_at_utc timestamps (None for empty)
- plan + per-market dict are deterministic across re-invocations (sorted markets)

Per anti-pattern A22, no audit events are emitted here — the policy is a
pure function and ``audit_event`` is asserted as data on the
``CalibrationPlan``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

import pytest

from services.audit.event_types import AuditEventType
from services.calibration.audit_events import (
    SLIPPAGE_CALIBRATION_RECALIBRATED_EVENT_TYPE,
)
from services.calibration.calibration import (
    BOOTSTRAP_ALPHA,
    BOOTSTRAP_BETA,
    BPS_PER_UNIT,
    MIN_OBSERVATIONS_FOR_OLS,
    CalibrationError,
    CalibrationFitMethod,
    CalibrationPlan,
    CalibrationTrigger,
    FillObservation,
    MarketCoefficient,
    PendingAuditEvent,
    plan_calibration_fit,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

#: Wall-clock for tests. tz-aware (anti-pattern A06).
TS: datetime = datetime(2026, 5, 10, 22, 0, tzinfo=UTC)

#: A stable UUIDv7 stand-in. The planner does not generate UUIDs (caller
#: boundary), so any well-formed UUID works for assertions.
NEW_VERSION_ID: UUID = UUID("01876b29-aaaa-7c3d-9f1e-000000000001")
PARENT_VERSION_ID: UUID = UUID("01876b29-bbbb-7c3d-9f1e-000000000002")


def _fill(
    market: str = "MES",
    *,
    size: str = "1",
    adv: str = "10000",
    expected: str = "5000.00",
    realized: str = "5000.50",
    direction: Literal["buy", "sell"] = "buy",
    fill_at: datetime | None = None,
) -> FillObservation:
    """Construct a FillObservation with sensible string-Decimal defaults."""
    return FillObservation(
        market=market,
        order_size_contracts=Decimal(size),
        adv_30d_contracts=Decimal(adv),
        expected_price=Decimal(expected),
        realized_price=Decimal(realized),
        direction=direction,
        fill_at_utc=fill_at if fill_at is not None else TS,
    )


def _stream_of_fills(
    market: str,
    n: int,
    *,
    base_size: str = "1",
    adv: str = "10000",
    expected: str = "5000.00",
    realized: str = "5000.50",
    direction: Literal["buy", "sell"] = "buy",
) -> tuple[FillObservation, ...]:
    """Build n identical fills for a market — useful for the constant-x branch."""
    return tuple(
        _fill(
            market=market,
            size=base_size,
            adv=adv,
            expected=expected,
            realized=realized,
            direction=direction,
            fill_at=datetime(2026, 5, 1 + (i % 28), 14, 30, tzinfo=UTC),
        )
        for i in range(n)
    )


# ---------------------------------------------------------------------------
# Locked-constant tests — assert spec values are exactly what the policy
# module exports. Drift here is a load-bearing change that must trip CI
# before any downstream test confuses the diagnosis.
# ---------------------------------------------------------------------------


class TestLockedConstants:
    def test_bootstrap_alpha_is_zero(self) -> None:
        assert BOOTSTRAP_ALPHA == Decimal("0")

    def test_bootstrap_beta_is_zero(self) -> None:
        assert BOOTSTRAP_BETA == Decimal("0")

    def test_bootstrap_constants_are_decimal_not_float(self) -> None:
        # Anti-pattern A05 would let a future PR set Decimal(0.0); reject.
        assert isinstance(BOOTSTRAP_ALPHA, Decimal)
        assert isinstance(BOOTSTRAP_BETA, Decimal)

    def test_min_observations_for_ols_is_30(self) -> None:
        # Aligns with backend-spec §2.14 "first real calibration after 30 days
        # of Phase 1 LIVE fills" — the per-market floor.
        assert MIN_OBSERVATIONS_FOR_OLS == 30

    def test_bps_per_unit_is_10000(self) -> None:
        assert BPS_PER_UNIT == Decimal("10000")


# ---------------------------------------------------------------------------
# Enum tests — mirror §3.12 CHECK + §3.30 audit taxonomy
# ---------------------------------------------------------------------------


class TestEnums:
    def test_calibration_trigger_values_match_schema_check(self) -> None:
        # §3.12 alembic: trigger CHECK (trigger IN ('bootstrap','scheduled_monthly',
        #   'scheduled_quarterly','manual'))
        assert {t.value for t in CalibrationTrigger} == {
            "bootstrap",
            "scheduled_monthly",
            "scheduled_quarterly",
            "manual",
        }

    def test_calibration_fit_method_values(self) -> None:
        assert {m.value for m in CalibrationFitMethod} == {"zero_prior", "ols"}

    def test_event_type_constant_matches_audit_enum(self) -> None:
        # The re-export in audit_events.py must equal the locked enum value.
        assert (
            SLIPPAGE_CALIBRATION_RECALIBRATED_EVENT_TYPE
            == AuditEventType.SLIPPAGE_CALIBRATION_RECALIBRATED.value
            == "slippage_calibration_recalibrated"
        )


# ---------------------------------------------------------------------------
# Plan-shape tests — frozen, slots, expected fields, version-no math
# ---------------------------------------------------------------------------


class TestPlanShape:
    def test_plan_is_frozen_dataclass(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert isinstance(plan, CalibrationPlan)
        with pytest.raises(AttributeError):
            plan.version_no = 99  # type: ignore[misc]

    def test_market_coefficient_is_frozen_dataclass(self) -> None:
        coef = MarketCoefficient(alpha=Decimal("0"), beta=Decimal("0"), n_obs=0, r_squared=None)
        with pytest.raises(AttributeError):
            coef.alpha = Decimal("1")  # type: ignore[misc]

    def test_pending_audit_event_is_frozen(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert isinstance(plan.audit_event, PendingAuditEvent)
        with pytest.raises(AttributeError):
            plan.audit_event.event_type = "wrong"  # type: ignore[misc]

    def test_version_no_with_no_parent_is_one(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert plan.version_no == 1

    def test_version_no_increments_from_parent(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            parent_version_id=PARENT_VERSION_ID,
            parent_version_no=7,
        )
        assert plan.version_no == 8

    def test_becomes_head_default_true(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert plan.becomes_head is True

    def test_becomes_head_passthrough_false(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.MANUAL,
            calibrated_at_utc=TS,
            becomes_head=False,
        )
        assert plan.becomes_head is False
        assert plan.audit_event.payload["becomes_head"] is False


# ---------------------------------------------------------------------------
# Bootstrap path — zero-prior, empty fills + non-empty bootstrap
# ---------------------------------------------------------------------------


class TestBootstrapPath:
    def test_bootstrap_with_empty_fills_returns_zero_prior_empty_dict(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert plan.fit_method == CalibrationFitMethod.ZERO_PRIOR
        assert plan.per_market_coefficients == {}
        assert plan.fill_count == 0
        assert plan.data_window_start_utc is None
        assert plan.data_window_end_utc is None
        assert plan.parent_version_id is None

    def test_bootstrap_with_fills_still_zero_prior(self) -> None:
        # BOOTSTRAP forces zero-prior even when fills exist (real Phase-1 call
        # path: dispatcher invokes BOOTSTRAP exactly once at first deploy).
        fills = _stream_of_fills("MES", 50)
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
            fills=fills,
        )
        assert plan.fit_method == CalibrationFitMethod.ZERO_PRIOR
        assert "MES" in plan.per_market_coefficients
        coef = plan.per_market_coefficients["MES"]
        assert coef.alpha == Decimal("0")
        assert coef.beta == Decimal("0")
        assert coef.n_obs == 50
        assert coef.r_squared is None

    def test_bootstrap_multi_market_zero_prior_per_market(self) -> None:
        fills = (*_stream_of_fills("MES", 5), *_stream_of_fills("ES", 12))
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
            fills=fills,
        )
        assert set(plan.per_market_coefficients.keys()) == {"ES", "MES"}
        assert plan.per_market_coefficients["MES"].n_obs == 5
        assert plan.per_market_coefficients["ES"].n_obs == 12
        for coef in plan.per_market_coefficients.values():
            assert coef.alpha == Decimal("0")
            assert coef.beta == Decimal("0")
            assert coef.r_squared is None


# ---------------------------------------------------------------------------
# Non-bootstrap fallback paths
# ---------------------------------------------------------------------------


class TestNonBootstrapFallback:
    def test_scheduled_monthly_with_no_fills_falls_back_to_zero_prior(self) -> None:
        # Defensive — should not raise; downstream caller decides whether to
        # abort. This matches the recon precedent where the planner is
        # tolerant of boundary inputs and the dispatcher is strict.
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
        )
        assert plan.fit_method == CalibrationFitMethod.ZERO_PRIOR
        assert plan.per_market_coefficients == {}
        assert plan.fill_count == 0

    def test_manual_with_few_fills_falls_back_to_zero_prior(self) -> None:
        fills = _stream_of_fills("MES", MIN_OBSERVATIONS_FOR_OLS - 1)  # 29 < 30
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.MANUAL,
            calibrated_at_utc=TS,
            fills=fills,
        )
        # Plan-level fit_method = zero_prior because EVERY market fell back.
        assert plan.fit_method == CalibrationFitMethod.ZERO_PRIOR
        coef = plan.per_market_coefficients["MES"]
        assert coef.alpha == Decimal("0")
        assert coef.beta == Decimal("0")
        assert coef.n_obs == 29
        assert coef.r_squared is None

    def test_mixed_market_one_ols_one_fallback_labels_plan_ols(self) -> None:
        # MES has 30 fills (just at threshold) → OLS; ES has 5 → zero-prior.
        # Plan-level fit_method = OLS because at least one market got a real fit.
        fills = (*_stream_of_fills("MES", 30), *_stream_of_fills("ES", 5))
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        assert plan.fit_method == CalibrationFitMethod.OLS
        assert plan.per_market_coefficients["MES"].r_squared is None  # constant-x branch
        # Even on the constant-x branch, alpha is the mean response; n_obs hits.
        assert plan.per_market_coefficients["MES"].n_obs == 30
        assert plan.per_market_coefficients["ES"].alpha == Decimal("0")
        assert plan.per_market_coefficients["ES"].beta == Decimal("0")
        assert plan.per_market_coefficients["ES"].n_obs == 5


# ---------------------------------------------------------------------------
# Slippage sign convention
# ---------------------------------------------------------------------------


class TestSlippageSignConvention:
    def test_buy_above_expected_yields_positive_signed_slippage(self) -> None:
        # 1 bp = 0.01% of 5000.00 = 0.50; realized = 5000.50, so 1 bp positive.
        # Build 30 identical fills; constant-x branch -> beta=0, alpha=mean(y)=+1 bp.
        fills = _stream_of_fills("MES", 30, expected="5000.00", realized="5000.50", direction="buy")
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        coef = plan.per_market_coefficients["MES"]
        assert coef.beta == Decimal("0")
        assert coef.alpha == Decimal("1")  # 1 bp

    def test_sell_below_expected_yields_positive_signed_slippage(self) -> None:
        # Sell 5000.00 expected, realized 4999.50 — adverse for seller, +1 bp.
        fills = _stream_of_fills(
            "MES", 30, expected="5000.00", realized="4999.50", direction="sell"
        )
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        coef = plan.per_market_coefficients["MES"]
        assert coef.alpha == Decimal("1")  # 1 bp

    def test_buy_below_expected_yields_negative_signed_slippage(self) -> None:
        # Price improvement on buy.
        fills = _stream_of_fills("MES", 30, expected="5000.00", realized="4999.50", direction="buy")
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        coef = plan.per_market_coefficients["MES"]
        assert coef.alpha == Decimal("-1")

    def test_sell_above_expected_yields_negative_signed_slippage(self) -> None:
        # Price improvement on sell.
        fills = _stream_of_fills(
            "MES", 30, expected="5000.00", realized="5000.50", direction="sell"
        )
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        coef = plan.per_market_coefficients["MES"]
        assert coef.alpha == Decimal("-1")


# ---------------------------------------------------------------------------
# Real OLS fit — known fixture
# ---------------------------------------------------------------------------


class TestOLSFit:
    def test_ols_fit_matches_hand_computed_regression(self) -> None:
        # Construct 30 fills with a clean linear relation:
        #   x_i = i / 30 (size i, ADV 30)
        #   y_i = 2 + 4 * x_i exactly (so beta should be 4, alpha should be 2, r2 = 1)
        # Use signed slippage = +1 bp baseline + extra; build expected/realized
        # to land each y_i exactly via the buy convention:
        #   y_bps = (realized - expected) / expected * 10000  (buy)
        # Pick expected = 10000 for clean math, then realized = expected * (1 + y_bps / 10000).
        expected_price = Decimal("10000")
        fills_list = []
        for i in range(1, 31):
            x_i = Decimal(i) / Decimal(30)
            y_bps = Decimal(2) + Decimal(4) * x_i
            realized = expected_price * (Decimal(1) + y_bps / BPS_PER_UNIT)
            fills_list.append(
                FillObservation(
                    market="MES",
                    order_size_contracts=Decimal(i),
                    adv_30d_contracts=Decimal(30),
                    expected_price=expected_price,
                    realized_price=realized,
                    direction="buy",
                    fill_at_utc=datetime(2026, 5, 1 + (i % 28), 14, 0, tzinfo=UTC),
                )
            )
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=tuple(fills_list),
        )
        coef = plan.per_market_coefficients["MES"]
        # Numerical: with Decimal arithmetic, beta should be exactly 4 and alpha exactly 2
        # within the Decimal context (no float accumulation). r_squared should be 1.
        assert coef.beta.quantize(Decimal("0.0001")) == Decimal("4.0000")
        assert coef.alpha.quantize(Decimal("0.0001")) == Decimal("2.0000")
        assert coef.r_squared is not None
        assert coef.r_squared.quantize(Decimal("0.0001")) == Decimal("1.0000")
        assert coef.n_obs == 30

    def test_ols_per_market_independent_fits(self) -> None:
        # Two markets with different relationships; both ≥ 30 fills.
        # MES: y = 1 + 2x, ES: y = -1 + 3x.
        def build(market: str, intercept: Decimal, slope: Decimal) -> list[FillObservation]:
            out = []
            for i in range(1, 31):
                x_i = Decimal(i) / Decimal(30)
                y_bps = intercept + slope * x_i
                realized = Decimal("10000") * (Decimal(1) + y_bps / BPS_PER_UNIT)
                out.append(
                    FillObservation(
                        market=market,
                        order_size_contracts=Decimal(i),
                        adv_30d_contracts=Decimal(30),
                        expected_price=Decimal("10000"),
                        realized_price=realized,
                        direction="buy",
                        fill_at_utc=datetime(2026, 5, 1 + (i % 28), 14, 0, tzinfo=UTC),
                    )
                )
            return out

        fills = (
            *build("MES", Decimal("1"), Decimal("2")),
            *build("ES", Decimal("-1"), Decimal("3")),
        )
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        assert plan.fit_method == CalibrationFitMethod.OLS
        mes = plan.per_market_coefficients["MES"]
        es = plan.per_market_coefficients["ES"]
        assert mes.alpha.quantize(Decimal("0.001")) == Decimal("1.000")
        assert mes.beta.quantize(Decimal("0.001")) == Decimal("2.000")
        assert es.alpha.quantize(Decimal("0.001")) == Decimal("-1.000")
        assert es.beta.quantize(Decimal("0.001")) == Decimal("3.000")


# ---------------------------------------------------------------------------
# Degenerate-OLS branches
# ---------------------------------------------------------------------------


class TestDegenerateOLSBranches:
    def test_zero_variance_regressor_returns_alpha_eq_y_bar_beta_zero(self) -> None:
        # All 30 fills have identical size/ADV -> Sxx == 0. beta must be 0,
        # alpha = ybar, r_squared must be None.
        fills = _stream_of_fills("MES", 30, expected="10000", realized="10001", direction="buy")
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        coef = plan.per_market_coefficients["MES"]
        assert coef.beta == Decimal("0")
        # +1 bp = (10001 - 10000) / 10000 * 10000 = 1
        assert coef.alpha == Decimal("1")
        assert coef.r_squared is None

    def test_zero_variance_response_with_real_regressor_returns_r_squared_none(
        self,
    ) -> None:
        # Vary x (size 1..30) but make every y_bps identical (constant slippage
        # per fill regardless of size) — Syy = 0, r_squared is None.
        fills_list = []
        for i in range(1, 31):
            fills_list.append(
                FillObservation(
                    market="MES",
                    order_size_contracts=Decimal(i),
                    adv_30d_contracts=Decimal(30),
                    expected_price=Decimal("10000"),
                    realized_price=Decimal("10001"),  # constant +1 bp regardless of i
                    direction="buy",
                    fill_at_utc=datetime(2026, 5, 1 + (i % 28), 14, 0, tzinfo=UTC),
                )
            )
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=tuple(fills_list),
        )
        coef = plan.per_market_coefficients["MES"]
        # beta should be 0 (since all y identical, OLS fit is the constant ybar).
        # alpha should be 1 bp.
        assert coef.beta.quantize(Decimal("0.0001")) == Decimal("0.0000")
        assert coef.alpha.quantize(Decimal("0.0001")) == Decimal("1.0000")
        assert coef.r_squared is None  # Syy == 0 branch


# ---------------------------------------------------------------------------
# Audit-event payload shape
# ---------------------------------------------------------------------------


class TestAuditEventPayload:
    def test_event_type_is_locked_taxonomy_string(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert plan.audit_event.event_type == "slippage_calibration_recalibrated"
        assert plan.audit_event.event_type == AuditEventType.SLIPPAGE_CALIBRATION_RECALIBRATED.value

    def test_payload_has_canonical_field_set(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.MANUAL,
            calibrated_at_utc=TS,
            parent_version_id=PARENT_VERSION_ID,
            parent_version_no=3,
            notes="op-init",
        )
        payload = plan.audit_event.payload
        assert set(payload.keys()) == {
            "new_version_id",
            "version_no",
            "trigger",
            "fit_method",
            "per_market_coefficients",
            "data_window_start_utc",
            "data_window_end_utc",
            "fill_count",
            "parent_version_id",
            "becomes_head",
            "notes",
            "ts",
        }

    def test_payload_uuid_fields_serialized_as_strings(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.MANUAL,
            calibrated_at_utc=TS,
            parent_version_id=PARENT_VERSION_ID,
            parent_version_no=1,
        )
        assert plan.audit_event.payload["new_version_id"] == str(NEW_VERSION_ID)
        assert plan.audit_event.payload["parent_version_id"] == str(PARENT_VERSION_ID)

    def test_payload_parent_version_id_none_serializes_to_none(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert plan.audit_event.payload["parent_version_id"] is None

    def test_payload_decimal_values_are_strings_per_a05(self) -> None:
        # Use OLS path so coefficients are non-trivial; bootstrap zeros
        # would also pass but trivially.
        fills = _stream_of_fills("MES", 30)
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
        )
        per_market = plan.audit_event.payload["per_market_coefficients"]
        assert "MES" in per_market
        coef_payload = per_market["MES"]
        assert isinstance(coef_payload["alpha"], str)
        assert isinstance(coef_payload["beta"], str)
        assert isinstance(coef_payload["n_obs"], int)
        # r_squared can be None or str — either is allowed.
        assert coef_payload["r_squared"] is None or isinstance(coef_payload["r_squared"], str)

    def test_payload_ts_is_isoformat_with_tz(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        ts = plan.audit_event.payload["ts"]
        assert ts == TS.isoformat()
        assert "+00:00" in ts

    def test_payload_data_window_matches_min_max_fill_timestamps(self) -> None:
        early = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
        late = datetime(2026, 5, 9, 18, 0, tzinfo=UTC)
        mid = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        fills = (
            _fill(market="MES", fill_at=mid),
            _fill(market="MES", fill_at=early),
            _fill(market="MES", fill_at=late),
        )
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,  # bootstrap so zero-prior; window math still runs
            calibrated_at_utc=TS,
            fills=fills,
        )
        assert plan.data_window_start_utc == early
        assert plan.data_window_end_utc == late
        assert plan.audit_event.payload["data_window_start_utc"] == early.isoformat()
        assert plan.audit_event.payload["data_window_end_utc"] == late.isoformat()

    def test_payload_data_window_none_when_no_fills(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert plan.audit_event.payload["data_window_start_utc"] is None
        assert plan.audit_event.payload["data_window_end_utc"] is None

    def test_payload_fill_count_equals_total(self) -> None:
        fills = (*_stream_of_fills("MES", 7), *_stream_of_fills("ES", 4))
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
            fills=fills,
        )
        assert plan.fill_count == 11
        assert plan.audit_event.payload["fill_count"] == 11

    def test_payload_notes_passes_through(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.MANUAL,
            calibrated_at_utc=TS,
            notes="manual recal after IBKR outage 2026-05-09",
        )
        assert plan.audit_event.payload["notes"] == "manual recal after IBKR outage 2026-05-09"

    def test_payload_notes_none_when_not_provided(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
        )
        assert plan.audit_event.payload["notes"] is None

    def test_payload_trigger_and_fit_method_are_enum_values(self) -> None:
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_QUARTERLY,
            calibrated_at_utc=TS,
        )
        assert plan.audit_event.payload["trigger"] == "scheduled_quarterly"
        assert plan.audit_event.payload["fit_method"] == "zero_prior"

    def test_per_market_coefficients_keys_sorted(self) -> None:
        # Determinism: dict preserves insertion order in Py3.7+; we sort markets
        # alphabetically so the audit payload is byte-stable across runs.
        fills = (*_stream_of_fills("ZZZ", 3), *_stream_of_fills("AAA", 3))
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
            fills=fills,
        )
        keys = list(plan.audit_event.payload["per_market_coefficients"].keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Determinism / re-invocation stability
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_produce_equal_plans(self) -> None:
        fills = _stream_of_fills("MES", 30)
        plan_a = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
            parent_version_id=PARENT_VERSION_ID,
            parent_version_no=2,
        )
        plan_b = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.SCHEDULED_MONTHLY,
            calibrated_at_utc=TS,
            fills=fills,
            parent_version_id=PARENT_VERSION_ID,
            parent_version_no=2,
        )
        assert plan_a == plan_b

    def test_same_inputs_produce_equal_payload_dicts(self) -> None:
        fills = _stream_of_fills("MES", 5)
        plan_a = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
            fills=fills,
        )
        plan_b = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
            fills=fills,
        )
        assert plan_a.audit_event.payload == plan_b.audit_event.payload


# ---------------------------------------------------------------------------
# Iterable input — generator support
# ---------------------------------------------------------------------------


class TestIterableInput:
    def test_generator_input_consumed_once(self) -> None:
        # Caller may pass a generator; planner must internalize to a tuple
        # so it can be iterated multiple times for window math + fit.
        def gen() -> Iterator[FillObservation]:
            yield from _stream_of_fills("MES", 3)

        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=TS,
            fills=gen(),
        )
        assert plan.fill_count == 3
        assert plan.data_window_start_utc is not None
        assert plan.data_window_end_utc is not None


# ---------------------------------------------------------------------------
# Validation errors — A05 + A06 + market/direction
# ---------------------------------------------------------------------------


class TestTimezoneEnforcement:
    def test_naive_calibrated_at_raises(self) -> None:
        naive = datetime(2026, 5, 10, 22, 0)  # no tzinfo
        with pytest.raises(CalibrationError, match=r"calibrated_at_utc must be timezone-aware"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=naive,
            )

    def test_naive_fill_at_raises(self) -> None:
        bad = _fill(fill_at=datetime(2026, 5, 1, 14, 30))  # no tzinfo
        with pytest.raises(CalibrationError, match=r"fill_at_utc must be timezone-aware"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_non_utc_offset_accepted(self) -> None:
        # tz-aware non-UTC is acceptable — A06 only requires tz-aware. The
        # caller is responsible for UTC-conversion at the data boundary; the
        # planner echoes the offset through unchanged.
        et = timezone(timedelta(hours=-5))
        ts_et = datetime(2026, 5, 10, 17, 0, tzinfo=et)
        plan = plan_calibration_fit(
            new_version_id=NEW_VERSION_ID,
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=ts_et,
        )
        assert plan.audit_event.payload["ts"] == ts_et.isoformat()


class TestNumericValidation:
    def test_non_decimal_size_raises_a05(self) -> None:
        # Construct a fill that bypasses static type by passing a float; A05
        # rejection happens at runtime in _validate_fill.
        bad = FillObservation(
            market="MES",
            order_size_contracts=1.0,  # type: ignore[arg-type]
            adv_30d_contracts=Decimal("100"),
            expected_price=Decimal("100"),
            realized_price=Decimal("100"),
            direction="buy",
            fill_at_utc=TS,
        )
        with pytest.raises(CalibrationError, match=r"order_size_contracts must be Decimal"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_non_decimal_adv_raises_a05(self) -> None:
        bad = FillObservation(
            market="MES",
            order_size_contracts=Decimal("1"),
            adv_30d_contracts=10000,  # type: ignore[arg-type]
            expected_price=Decimal("100"),
            realized_price=Decimal("100"),
            direction="buy",
            fill_at_utc=TS,
        )
        with pytest.raises(CalibrationError, match=r"adv_30d_contracts must be Decimal"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_zero_adv_raises(self) -> None:
        bad = _fill(adv="0")
        with pytest.raises(CalibrationError, match=r"adv_30d_contracts must be > 0"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_negative_size_raises(self) -> None:
        bad = _fill(size="-1")
        with pytest.raises(CalibrationError, match=r"order_size_contracts must be > 0"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_zero_expected_price_raises(self) -> None:
        bad = _fill(expected="0")
        with pytest.raises(CalibrationError, match=r"expected_price must be > 0"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_negative_realized_price_raises(self) -> None:
        bad = _fill(realized="-1")
        with pytest.raises(CalibrationError, match=r"realized_price must be > 0"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_empty_market_string_raises(self) -> None:
        bad = _fill(market="")
        with pytest.raises(CalibrationError, match=r"market must be a non-empty string"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )

    def test_invalid_direction_raises(self) -> None:
        bad = FillObservation(
            market="MES",
            order_size_contracts=Decimal("1"),
            adv_30d_contracts=Decimal("100"),
            expected_price=Decimal("100"),
            realized_price=Decimal("100"),
            direction="hold",  # type: ignore[arg-type]
            fill_at_utc=TS,
        )
        with pytest.raises(CalibrationError, match=r"direction must be 'buy' or 'sell'"):
            plan_calibration_fit(
                new_version_id=NEW_VERSION_ID,
                trigger=CalibrationTrigger.BOOTSTRAP,
                calibrated_at_utc=TS,
                fills=(bad,),
            )


# ---------------------------------------------------------------------------
# Module-contract surface — re-export discipline + __all__ wholeness
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_audit_events_re_export_imports_cleanly(self) -> None:
        from services.calibration import audit_events as ae

        # The constant + TypedDict shape must be importable from the
        # audit_events module so the future dispatcher has one place to
        # import the wire shape from.
        assert ae.SLIPPAGE_CALIBRATION_RECALIBRATED_EVENT_TYPE == (
            "slippage_calibration_recalibrated"
        )

    def test_calibration_module_all_contains_public_surface(self) -> None:
        from services.calibration import calibration as cal

        # The plan-then-apply contract surface must be in __all__ so a star
        # import from a downstream module gets the full plan API.
        assert "plan_calibration_fit" in cal.__all__
        assert "CalibrationPlan" in cal.__all__
        assert "CalibrationTrigger" in cal.__all__
        assert "CalibrationFitMethod" in cal.__all__
        assert "FillObservation" in cal.__all__
        assert "MarketCoefficient" in cal.__all__
        assert "PendingAuditEvent" in cal.__all__
        assert "CalibrationError" in cal.__all__
