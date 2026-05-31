"""Pydantic-side tolerance tests for the ``market_evaluations`` field added
to ``LeanEventRequest`` by PR-A of ``Docs/signal-proximity-design.md``.

These tests live in their own file so the PR-A diff is self-contained;
the broader heartbeat-shape regression suite remains in
``test_api_lean_schema.py``. Focus here: a new optional field MUST
gracefully accept absence (so older LEAN deploys keep validating) AND
accept a well-formed array (so the PR-A LEAN deploy doesn't 422).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from services.api.schemas.lean import LeanEventRequest


def _base_heartbeat(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": "lean_cycle_heartbeat",
        "ts_utc": datetime(2026, 5, 28, 21, 30, tzinfo=UTC).isoformat(),
        "algorithm_id": "v1_trend_following",
        "session_date_et": "2026-05-28",
        "equity_usd": "100000.00",
        "live_mode": True,
        "signals_emitted_count": 0,
        "rejections_count": 10,
    }
    payload.update(overrides)
    return payload


def _proximity_row(market: str = "/MES") -> dict[str, object]:
    """A representative per-market proximity row matching the LEAN wire shape.

    Mirrors ``lean/v1_strategy.py::_serialize_market_proximity`` output —
    everything as strings except None, gate states as enum values.
    """
    return {
        "market": market,
        "long_donchian": {"state": "close", "headroom": "0.008", "detail": None},
        "short_donchian": {"state": "fail", "headroom": "0.085", "detail": None},
        "long_trend": {"state": "pass", "headroom": "0.015", "detail": None},
        "short_trend": {"state": "fail", "headroom": "-0.015", "detail": None},
        "hurst": {"state": "pass", "headroom": "0.03", "detail": None},
        "last_close": "5234.50",
        "overall_state": "close",
        "closest_gate": "donchian",
        "gate_status": "ok",
    }


# ---------------------------------------------------------------------------
# Acceptance — the new field round-trips when supplied
# ---------------------------------------------------------------------------


def test_heartbeat_with_market_evaluations_parses() -> None:
    request = LeanEventRequest.model_validate(
        _base_heartbeat(market_evaluations=[_proximity_row("/MES"), _proximity_row("/MNQ")])
    )
    assert request.market_evaluations is not None
    assert len(request.market_evaluations) == 2
    # PR-B tightened the type from list[dict[str, Any]] to a typed
    # MarketEvaluationItem — access fields via attribute (the route
    # handler in lean.py walks the same attributes when persisting).
    assert request.market_evaluations[0].market == "/MES"
    assert request.market_evaluations[1].market == "/MNQ"
    assert request.market_evaluations[0].overall_state == "close"
    assert request.market_evaluations[0].closest_gate == "donchian"
    assert request.market_evaluations[0].gate_status == "ok"
    # Per-gate sub-objects round-trip as typed GateProximityItem.
    assert request.market_evaluations[0].long_donchian.state == "close"
    assert request.market_evaluations[0].long_donchian.headroom == Decimal("0.008")


def test_heartbeat_with_empty_market_evaluations_parses() -> None:
    """Day-of-deploy LEAN may briefly send an empty array — must still 202."""
    request = LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=[]))
    assert request.market_evaluations == []


# ---------------------------------------------------------------------------
# Backwards compatibility — older LEAN deploys MUST NOT 422
# ---------------------------------------------------------------------------


def test_legacy_heartbeat_without_market_evaluations_still_parses() -> None:
    """Pre-PR-A LEAN images don't know about market_evaluations.

    The model MUST treat the absent field as None — anything else (e.g.,
    ``extra="forbid"`` rejecting unknown fields, or making the field
    required) would 422 every heartbeat from a stale container and silence
    the liveness probe.
    """
    request = LeanEventRequest.model_validate(_base_heartbeat())
    assert request.market_evaluations is None


def test_heartbeat_with_explicit_none_market_evaluations_parses() -> None:
    request = LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=None))
    assert request.market_evaluations is None


def test_minimal_heartbeat_without_any_extras_still_parses() -> None:
    """The bare-minimum heartbeat shape MUST keep working.

    Locks the contract for any LEAN release line that drops the optional
    extras (counters, error, market_evaluations) — only the canonical
    event_type + ts_utc + algorithm_id are required.
    """
    request = LeanEventRequest.model_validate(
        {
            "event_type": "lean_cycle_heartbeat",
            "ts_utc": datetime(2026, 5, 28, 21, 30, tzinfo=UTC).isoformat(),
            "algorithm_id": "v1_trend_following",
        }
    )
    assert request.market_evaluations is None
    assert request.signals_emitted_count is None
    assert request.rejections_count is None


# ---------------------------------------------------------------------------
# Validation — wrong shapes still rejected
# ---------------------------------------------------------------------------


def test_market_evaluations_as_dict_rejected() -> None:
    """The field is typed as a list — passing a dict must 422.

    Guards against a future LEAN bug serializing the per-market collection
    as a dict keyed by market name. The route handler logs the
    market_evaluations count via ``len(...)``, which would TypeError on a
    dict — better to reject at the schema layer.
    """
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(
            _base_heartbeat(market_evaluations={"/MES": _proximity_row("/MES")})
        )


def test_market_evaluations_as_scalar_rejected() -> None:
    """A scalar (e.g., a stray int) must 422 rather than coerce."""
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=42))


def test_market_evaluations_member_must_be_object() -> None:
    """Each list member must be a dict-shaped object (extras=Any allowed)."""
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=["not_a_dict"]))


def test_extra_unknown_field_still_forbidden() -> None:
    """The new optional field doesn't loosen ``extra='forbid'``.

    Sanity check: arbitrary unknown top-level fields still 422 — this is
    the locked behavior for the LeanEventRequest model and PR-A only
    EXTENDS the allowed-field set, not the policy.
    """
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(this_does_not_exist=1))


# ---------------------------------------------------------------------------
# PR-B tightening — the per-market shape is now a typed nested model
# ---------------------------------------------------------------------------


def test_market_evaluation_invalid_gate_state_rejected() -> None:
    """A gate state not in ('pass','close','fail') must 422.

    Guards against drift between the LEAN serializer's ``GateState`` enum
    and the api's tightened model. A new state value MUST land here + in
    the migration's CHECK constraint together, or the api rejects.
    """
    row = _proximity_row("/MES")
    row["long_donchian"] = {
        "state": "maybe",  # not a valid GateState
        "headroom": "0.005",
        "detail": None,
    }
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=[row]))


def test_market_evaluation_invalid_overall_state_rejected() -> None:
    """The overall_state literal is enforced separately from per-gate state."""
    row = _proximity_row("/MES")
    row["overall_state"] = "almost"
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=[row]))


def test_market_evaluation_invalid_closest_gate_rejected() -> None:
    """closest_gate must be one of donchian / trend / hurst / history."""
    row = _proximity_row("/MES")
    row["closest_gate"] = "donchian_high"  # close to a real value but wrong
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=[row]))


def test_market_evaluation_invalid_gate_status_rejected() -> None:
    """gate_status must match the GateStatus literal in proximity.py.

    This is the carry-over watch-out from PR-A's risk-review — the API
    schema's gate_status alphabet MUST equal the migration's CHECK
    constraint alphabet MUST equal proximity.GateStatus.
    """
    row = _proximity_row("/MES")
    row["gate_status"] = "paused"  # not in {ok, warming_up, decommissioned}
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=[row]))


def test_market_evaluation_unknown_per_gate_key_rejected() -> None:
    """The per-gate sub-object is extra='forbid' too.

    An unexpected key on a gate sub-object (e.g., a stale field name from
    a future strategy iteration) must 422 rather than be silently dropped.
    """
    row = _proximity_row("/MES")
    row["long_donchian"] = {
        "state": "pass",
        "headroom": "0.005",
        "detail": None,
        "extra_thing": True,
    }
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=[row]))


def test_market_evaluation_warming_up_with_none_numerics_parses() -> None:
    """Warming-up markets emit state='fail' + headroom=None for every gate.

    The api must accept the None values on the wire — this is the
    canonical "no data yet" shape per design §3.4.
    """
    row = {
        "market": "/MGC",
        "long_donchian": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "short_donchian": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "long_trend": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "short_trend": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "hurst": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "last_close": None,
        "overall_state": "fail",
        "closest_gate": "history",
        "gate_status": "warming_up",
    }
    request = LeanEventRequest.model_validate(_base_heartbeat(market_evaluations=[row]))
    assert request.market_evaluations is not None
    assert request.market_evaluations[0].gate_status == "warming_up"
    assert request.market_evaluations[0].last_close is None
    assert request.market_evaluations[0].long_donchian.headroom is None


# ===========================================================================
# position_exit_evaluations — PR-A of Docs/exit-proximity-design.md
#
# Same tolerance contract as market_evaluations: absence must NOT 422 (older
# LEAN deploys), a well-formed array round-trips, and malformed shapes /
# out-of-alphabet enum values are rejected at the schema boundary.
# ===========================================================================


def _exit_eval_row(market: str = "/MES", **overrides: object) -> dict[str, object]:
    """A representative per-position exit-proximity row matching the LEAN wire
    shape (``lean/v1_strategy.py::_serialize_position_exit_proximity``)."""
    row: dict[str, object] = {
        "market": market,
        "direction": "long",
        "held_days": 20,
        "trend_flip": {"state": "holding", "headroom": "0.10", "detail": None},
        "stop": {"state": "holding", "headroom": None, "detail": "no stop price on record"},
        "reversal": {"state": "holding", "headroom": None, "detail": None},
        "decommission": {"state": "holding", "headroom": None, "detail": None},
        "last_close": "164.25",
        "stop_price": None,
        "overall_state": "holding",
        "closest_exit": "trend_flip",
        "gate_status": "active",
    }
    row.update(overrides)
    return row


def test_heartbeat_with_position_exit_evaluations_parses() -> None:
    request = LeanEventRequest.model_validate(
        _base_heartbeat(position_exit_evaluations=[_exit_eval_row("/MES"), _exit_eval_row("/M2K")])
    )
    assert request.position_exit_evaluations is not None
    assert len(request.position_exit_evaluations) == 2
    row = request.position_exit_evaluations[0]
    assert row.market == "/MES"
    assert row.direction == "long"
    assert row.held_days == 20
    assert row.trend_flip.state == "holding"
    assert row.trend_flip.headroom == Decimal("0.10")
    assert row.stop.headroom is None
    assert row.overall_state == "holding"
    assert row.closest_exit == "trend_flip"
    assert row.gate_status == "active"


def test_heartbeat_with_empty_position_exit_evaluations_parses() -> None:
    request = LeanEventRequest.model_validate(_base_heartbeat(position_exit_evaluations=[]))
    assert request.position_exit_evaluations == []


def test_legacy_heartbeat_without_position_exit_evaluations_is_none() -> None:
    """Pre-PR-A LEAN images don't send the field — must parse as None, not 422."""
    request = LeanEventRequest.model_validate(_base_heartbeat())
    assert request.position_exit_evaluations is None


def test_heartbeat_with_both_proximity_arrays_parses() -> None:
    """Entry + exit proximity ride the same heartbeat — both round-trip."""
    request = LeanEventRequest.model_validate(
        _base_heartbeat(
            market_evaluations=[_proximity_row("/MES")],
            position_exit_evaluations=[_exit_eval_row("/MES")],
        )
    )
    assert request.market_evaluations is not None
    assert request.position_exit_evaluations is not None
    assert len(request.market_evaluations) == 1
    assert len(request.position_exit_evaluations) == 1


def test_position_exit_invalid_trigger_state_rejected() -> None:
    """A trigger state not in ('holding','near','triggered') must 422."""
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(
            _base_heartbeat(
                position_exit_evaluations=[
                    _exit_eval_row(trend_flip={"state": "maybe", "headroom": None, "detail": None})
                ]
            )
        )


def test_position_exit_invalid_closest_exit_rejected() -> None:
    """closest_exit must be one of stop / reversal / trend_flip / decommission."""
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(
            _base_heartbeat(position_exit_evaluations=[_exit_eval_row(closest_exit="stop_hit")])
        )


def test_position_exit_invalid_gate_status_rejected() -> None:
    """gate_status alphabet MUST match exit_proximity.GateStatus (the migration
    CHECK constraint in PR-B will be pinned to the same set)."""
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(
            _base_heartbeat(position_exit_evaluations=[_exit_eval_row(gate_status="paused")])
        )


def test_position_exit_invalid_direction_rejected() -> None:
    """direction is long|short only (FLAT positions never produce a row)."""
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(
            _base_heartbeat(position_exit_evaluations=[_exit_eval_row(direction="flat")])
        )


def test_position_exit_unknown_trigger_key_rejected() -> None:
    """The per-trigger sub-object is extra='forbid' too."""
    with pytest.raises(ValidationError):
        LeanEventRequest.model_validate(
            _base_heartbeat(
                position_exit_evaluations=[
                    _exit_eval_row(
                        stop={"state": "near", "headroom": "0.004", "detail": None, "oops": 1}
                    )
                ]
            )
        )


def test_position_exit_warming_up_null_headrooms_parses() -> None:
    """Warming-up / no-stop rows carry None headrooms + None snapshot fields."""
    row = _exit_eval_row(
        trend_flip={"state": "holding", "headroom": None, "detail": "warming_up"},
        last_close=None,
        gate_status="warming_up",
    )
    request = LeanEventRequest.model_validate(_base_heartbeat(position_exit_evaluations=[row]))
    assert request.position_exit_evaluations is not None
    assert request.position_exit_evaluations[0].gate_status == "warming_up"
    assert request.position_exit_evaluations[0].last_close is None
    assert request.position_exit_evaluations[0].trend_flip.headroom is None
