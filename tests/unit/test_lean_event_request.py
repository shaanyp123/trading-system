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
    assert request.market_evaluations[0]["market"] == "/MES"
    assert request.market_evaluations[1]["market"] == "/MNQ"


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
