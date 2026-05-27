"""Unit tests for ``services.api.schemas.lean.LeanEventRequest``.

Regression suite for the heartbeat-fields fix (post-pivot 2026-05-12):
LEAN's ``_post_event("lean_cycle_heartbeat", extra={...})`` includes
``signals_emitted_count`` + ``rejections_count`` + ``error`` per-cycle
summary fields. The model is ``extra="forbid"`` so before this fix
every heartbeat was rejected with 422 (observed in
``trading-lean_local`` logs as ``lean_signal_post_http_error
status=422 event_type=lean_cycle_heartbeat``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from services.api.schemas.lean import LeanEventRequest


def _base_heartbeat_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": "lean_cycle_heartbeat",
        "ts_utc": datetime(2026, 5, 12, 21, 30, tzinfo=UTC).isoformat(),
        "algorithm_id": "v1_trend_following",
        "session_date_et": "2026-05-12",
        "equity_usd": "100000.00",
        "live_mode": True,
        "signals_emitted_count": 0,
        "rejections_count": 0,
    }
    payload.update(overrides)
    return payload


class TestHeartbeatFieldsAccepted:
    """The 3 new fields parse cleanly on the heartbeat path."""

    def test_full_heartbeat_with_new_fields_parses(self) -> None:
        request = LeanEventRequest.model_validate(_base_heartbeat_payload())
        assert request.event_type == "lean_cycle_heartbeat"
        assert request.signals_emitted_count == 0
        assert request.rejections_count == 0
        assert request.error is None

    def test_signals_count_one_or_more(self) -> None:
        request = LeanEventRequest.model_validate(
            _base_heartbeat_payload(signals_emitted_count=3, rejections_count=2)
        )
        assert request.signals_emitted_count == 3
        assert request.rejections_count == 2

    def test_error_tag_carried(self) -> None:
        request = LeanEventRequest.model_validate(
            _base_heartbeat_payload(error="v1_params_build_failed")
        )
        assert request.error == "v1_params_build_failed"

    def test_fields_are_optional_for_legacy_payloads(self) -> None:
        """Old LEAN images without the per-cycle counters MUST still validate.

        Forward-compat for future LEAN versions that drop or rename the
        counters: the model accepts missing fields by treating them as
        None.
        """
        minimal = {
            "event_type": "lean_cycle_heartbeat",
            "ts_utc": datetime(2026, 5, 12, 21, 30, tzinfo=UTC).isoformat(),
            "algorithm_id": "v1_trend_following",
        }
        request = LeanEventRequest.model_validate(minimal)
        assert request.signals_emitted_count is None
        assert request.rejections_count is None
        assert request.error is None


class TestHeartbeatFieldsValidation:
    """Per-field bounds + type checks."""

    def test_negative_signals_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(_base_heartbeat_payload(signals_emitted_count=-1))

    def test_negative_rejections_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(_base_heartbeat_payload(rejections_count=-1))

    def test_error_over_512_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(_base_heartbeat_payload(error="x" * 513))


class TestExtraStillForbidden:
    """The fix doesn't loosen `extra="forbid"` — random unknown fields still 422."""

    def test_random_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(_base_heartbeat_payload(this_field_does_not_exist="hi"))


class TestStrategyInitializedPath:
    """The initialize path doesn't send the heartbeat fields; they remain None."""

    def test_strategy_initialized_minimal(self) -> None:
        request = LeanEventRequest.model_validate(
            {
                "event_type": "lean_strategy_initialized",
                "ts_utc": datetime(2026, 5, 12, 21, 30, tzinfo=UTC).isoformat(),
                "algorithm_id": "v1_trend_following",
                "live_mode": True,
            }
        )
        assert request.event_type == "lean_strategy_initialized"
        assert request.signals_emitted_count is None
        assert request.rejections_count is None


class TestSignalEmittedPathStillWorks:
    """Adding heartbeat fields doesn't break the signal_emitted path."""

    def test_signal_emitted_full_payload(self) -> None:
        request = LeanEventRequest.model_validate(
            {
                "event_type": "signal_emitted",
                "ts_utc": datetime(2026, 5, 12, 21, 30, tzinfo=UTC).isoformat(),
                "algorithm_id": "v1_trend_following",
                "session_date_et": "2026-05-12",
                "equity_usd": "100000.00",
                "live_mode": True,
                "market": "/MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": "5234.75",
                "sizing_trace": {"gross_dollars": "5234.75"},
                "strategy_version": "v1_trend_following@abc1234",
            }
        )
        assert request.event_type == "signal_emitted"
        assert request.market == "/MES"
        assert request.target_contracts == 1
        assert request.decision_price == Decimal("5234.75")


# ---------------------------------------------------------------------------
# PR-B exit-pipeline fields (added 2026-05-26 per exit-pipeline-design.md §5.2)
# ---------------------------------------------------------------------------


def _base_entry_signal_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": "signal_emitted",
        "ts_utc": datetime(2026, 5, 26, 21, 30, tzinfo=UTC).isoformat(),
        "algorithm_id": "v1_trend_following",
        "session_date_et": "2026-05-26",
        "equity_usd": "100000.00",
        "live_mode": True,
        "market": "/MES",
        "direction": "long",
        "target_contracts": 1,
        "decision_price": "5234.75",
        "sizing_trace": {"gross_dollars": "5234.75"},
        "strategy_version": "v1_trend_following@abc1234",
    }
    payload.update(overrides)
    return payload


def _base_exit_signal_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": "signal_emitted",
        "ts_utc": datetime(2026, 5, 26, 21, 30, tzinfo=UTC).isoformat(),
        "algorithm_id": "v1_trend_following",
        "session_date_et": "2026-05-26",
        "equity_usd": "100000.00",
        "live_mode": True,
        "market": "/MES",
        "direction": "flat",
        "target_contracts": 0,
        "decision_price": "5234.75",
        "sizing_trace": {"schema_version": 1},
        "strategy_version": "v1_trend_following@abc1234",
        "signal_type": "exit",
        "exit_reason": "trend_flip",
        "prior_position_direction": "long",
        "prior_position_quantity": 3,
    }
    payload.update(overrides)
    return payload


class TestSignalTypeDefaultsToEntry:
    """Backwards compat: pre-PR-B payloads without signal_type still parse."""

    def test_signal_type_omitted_defaults_to_entry(self) -> None:
        request = LeanEventRequest.model_validate(_base_entry_signal_payload())
        assert request.signal_type == "entry"
        assert request.exit_reason is None
        assert request.prior_position_direction is None
        assert request.prior_position_quantity is None
        assert request.paired_entry_market is None

    def test_signal_type_explicit_entry_parses(self) -> None:
        request = LeanEventRequest.model_validate(_base_entry_signal_payload(signal_type="entry"))
        assert request.signal_type == "entry"

    def test_invalid_signal_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(_base_entry_signal_payload(signal_type="bogus"))


class TestExitPayloadAccepted:
    """Exit payloads with the full PR-B field set parse cleanly."""

    def test_trend_flip_exit_payload_parses(self) -> None:
        request = LeanEventRequest.model_validate(_base_exit_signal_payload())
        assert request.signal_type == "exit"
        assert request.exit_reason == "trend_flip"
        assert request.prior_position_direction == "long"
        assert request.prior_position_quantity == 3
        assert request.paired_entry_market is None
        # direction is the sentinel 'flat' for exits
        assert request.direction == "flat"

    def test_reversal_exit_with_paired_entry_market(self) -> None:
        request = LeanEventRequest.model_validate(
            _base_exit_signal_payload(
                exit_reason="reversal",
                paired_entry_market="/MES",
            )
        )
        assert request.exit_reason == "reversal"
        assert request.paired_entry_market == "/MES"

    def test_decommission_exit(self) -> None:
        request = LeanEventRequest.model_validate(
            _base_exit_signal_payload(exit_reason="decommission")
        )
        assert request.exit_reason == "decommission"

    def test_short_prior_position_accepted(self) -> None:
        request = LeanEventRequest.model_validate(
            _base_exit_signal_payload(
                prior_position_direction="short",
                prior_position_quantity=-5,
            )
        )
        assert request.prior_position_direction == "short"
        assert request.prior_position_quantity == -5


class TestExitFieldValidation:
    """Per-field bounds + type checks for the new exit fields."""

    def test_invalid_exit_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(_base_exit_signal_payload(exit_reason="bogus"))

    def test_invalid_prior_position_direction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(
                _base_exit_signal_payload(prior_position_direction="sideways")
            )

    def test_paired_entry_market_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(
                _base_exit_signal_payload(
                    exit_reason="reversal",
                    paired_entry_market="",
                )
            )

    def test_paired_entry_market_over_32_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LeanEventRequest.model_validate(
                _base_exit_signal_payload(
                    exit_reason="reversal",
                    paired_entry_market="x" * 33,
                )
            )
