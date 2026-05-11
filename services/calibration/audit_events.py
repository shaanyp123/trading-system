"""Audit-event payload shape for slippage-calibration recalibration.

Single event_type — ``slippage_calibration_recalibrated`` — fired once per
:func:`services.calibration.calibration.plan_calibration_fit` invocation
(the dispatcher writes it via ``services.audit.writer.append_audit_event``
with the payload built by the planner).

This module is a typed contract over what the planner emits. It does NOT
re-derive the payload — that's :func:`services.calibration.calibration._build_audit_event`'s
job. It's a TypedDict + a re-export of the locked enum value so the
dispatcher (Week 4 work) and the chain-verifier downstream consumers have
one place to import the wire shape.

Anti-pattern reference (``Docs/claude-dev-guide.md §11``):

- A04 — DO NOT introduce a new audit event_type without updating the
  locked taxonomy. ``slippage_calibration_recalibrated`` IS in the locked
  taxonomy at ``services/audit/event_types.py:107``; this module just
  shapes the payload that gets written under it.
- A05 — Decimal serialized as string in the payload (the planner's
  ``_coefficients_to_payload`` enforces this; the TypedDict declares the
  string types).
"""

from __future__ import annotations

from typing import Final, Literal, TypedDict

from services.audit.event_types import AuditEventType

#: The locked event-type wire identifier for slippage-calibration writes.
#: Mirrors ``services/audit/event_types.py:107`` for grep-ability + so a
#: caller importing only this module gets the canonical string without an
#: indirect import through ``services/audit/``.
SLIPPAGE_CALIBRATION_RECALIBRATED_EVENT_TYPE: Final[str] = (
    AuditEventType.SLIPPAGE_CALIBRATION_RECALIBRATED.value
)


class MarketCoefficientPayload(TypedDict):
    """Per-market entry inside ``per_market_coefficients`` JSONB.

    Mirrors the §3.12 example shape exactly:

        ``{"alpha": "0.5", "beta": "12.3", "n_obs": 84, "r_squared": "0.61"}``

    Decimal values serialized as strings (A05); ``r_squared`` is
    ``None`` for zero-prior + zero-variance fits (see
    :class:`services.calibration.calibration.MarketCoefficient`).
    """

    alpha: str
    beta: str
    n_obs: int
    r_squared: str | None


class SlippageCalibrationRecalibratedPayload(TypedDict):
    """Payload for ``event_type=slippage_calibration_recalibrated``.

    The full set of fields the planner's
    :func:`services.calibration.calibration._build_audit_event` writes.
    Ordered to mirror the reading order a chain-verifier walking the
    JSONB output would expect: identity first, then fit, then provenance,
    then operational metadata.

    - ``new_version_id``: caller-generated UUIDv7, the
      ``slippage_calibration_versions.id`` to be INSERTed by the
      dispatcher AFTER this event lands.
    - ``version_no``: monotonic next free integer (``parent + 1`` or 1).
    - ``trigger``: one of the four
      :class:`services.calibration.calibration.CalibrationTrigger` values.
    - ``fit_method``: one of the two
      :class:`services.calibration.calibration.CalibrationFitMethod`
      values; plan-level label (per-market label is implicit in
      ``per_market_coefficients[market]['r_squared']``).
    - ``per_market_coefficients``: the §3.12 JSONB content.
    - ``data_window_start_utc`` / ``data_window_end_utc``: ISO 8601
      with tz suffix (or None for the empty-fills bootstrap).
    - ``fill_count``: total number of :class:`FillObservation` rows the
      planner consumed (sum of ``n_obs`` across markets).
    - ``parent_version_id``: prior HEAD's
      ``slippage_calibration_versions.id`` as string (or None on first
      bootstrap).
    - ``becomes_head``: dispatcher's signal of whether to swap the HEAD
      pointer post-INSERT. Default True for the operational cron.
    - ``notes``: free-form operational text from the trigger context
      (e.g. ``"first quarterly recalibration after Phase 2 cutover"``).
    - ``ts``: ``calibrated_at_utc`` ISO 8601.
    """

    new_version_id: str
    version_no: int
    trigger: Literal["bootstrap", "scheduled_monthly", "scheduled_quarterly", "manual"]
    fit_method: Literal["zero_prior", "ols"]
    per_market_coefficients: dict[str, MarketCoefficientPayload]
    data_window_start_utc: str | None
    data_window_end_utc: str | None
    fill_count: int
    parent_version_id: str | None
    becomes_head: bool
    notes: str | None
    ts: str


__all__ = [
    "SLIPPAGE_CALIBRATION_RECALIBRATED_EVENT_TYPE",
    "MarketCoefficientPayload",
    "SlippageCalibrationRecalibratedPayload",
]
