"""services/risk/dispatch.py — I/O orchestrator for risk state-machine plans.

The companion to :mod:`services.risk.state_machine`: that module is pure
policy (plan generation); this module owns the I/O — writing audit events
via :func:`services.audit.writer.append_audit_event`, UPSERTing the
``risk_state`` row, and returning the state-transition's
``audit_event_uuid`` so the route layer can emit the SSE envelope with the
correct linkage.

The split mirrors the plan-then-apply convention used by
:mod:`services.risk.sizing` and :mod:`services.reconciliation.recon`:

* **Policy** (``state_machine``) is a synchronous, deterministic function
  that returns a :class:`StateTransitionPlan`. Unit tests stay
  testcontainer-free (anti-pattern A22).
* **I/O** (this module) consumes the plan and executes the writes against
  a real :class:`AsyncSession`. Exercised by
  :mod:`tests.integration.test_kill_switch_end_to_end` against a Postgres
  testcontainer.

Audit-first ordering
====================

The writer commits its own transaction internally; we do NOT chain the
``risk_state`` UPSERT into the audit write's SERIALIZABLE transaction.
Reasons:

* :func:`append_audit_event` enforces SERIALIZABLE + ``pg_advisory_xact_lock``
  + 5-attempt retry. Wrapping a second UPSERT inside that block would
  expand the lock's hold time AND tie the UPSERT's failure mode to the
  audit chain's serialization-failure handling — both undesirable.
* If the audit writes succeed but the ``risk_state`` UPSERT fails, the
  audit log carries the canonical record. The ``risk_state`` row is a
  read-side cache for the API layer; recovery is a manual repair
  (UPDATE ``risk_state`` to match the latest ``state_transition_*`` audit
  row). Backend-spec §2.10.1 names the audit log as the authoritative
  source for state — this matches.
* The reverse ordering (UPSERT first, then audit) is strictly worse: it
  would leave the live state ahead of the audit chain, which the spec
  forbids.

Why no SSE emit here
====================

Emitting SSE from this module would force a dependency on
:mod:`services.api.sse`, which is in the api layer. Keeping the risk
layer free of api dependencies preserves the option to call the same
dispatch entry-point from non-api callers (a future scheduler worker, a
Discord bot path that hits the DB directly). The route handler that
called us is responsible for the SSE emit — it knows the user's session
context and the right time to fan-out.

A01 BINDS — every audit row routes through :func:`append_audit_event`.
A02 BINDS — this module lives under ``services/risk/``; ``risk-review-approved``
required to merge changes.
A04 BINDS — every emitted ``event_type`` is in the locked taxonomy
(see :class:`services.audit.event_types.AuditEventType`).
A05 N/A (no monetary values).
A06 enforced — all timestamps are tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.risk.state_machine import StateTransitionPlan

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class AppliedStateTransition:
    """Outcome of executing a :class:`StateTransitionPlan`.

    Returned by :func:`apply_state_transition` so the route layer can:

    1. Echo ``state_transition_audit_event_uuid`` in the HTTP response body
       (operators deep-link from API responses into the audit explorer).
    2. Populate the SSE envelope's ``data.audit_event_uuid`` field so the
       frontend can correlate the live event with the audit row.
    """

    state_transition_audit_event_uuid: UUID
    """The audit_log.event_uuid of the *state_transition_** row — the LAST
    event in plan.audit_events. When the plan emits multiple audit events
    (e.g. CONVALESCENT→HALT_NEW emits ``convalescent_counter_reset`` BEFORE
    ``state_transition_normal_to_halt``), this is the UUID of the state-
    transition row itself."""

    new_state: Literal["NORMAL", "HALT_NEW", "CONVALESCENT"]
    """The string value of ``plan.new_state`` (echoed for the route's
    response body convenience — the route already has the plan, but
    callers reading this dataclass alone benefit from the explicit field)."""

    new_severity: Literal["routine", "defensive_envelope", "incident_review"] | None
    """The string value of ``plan.new_severity``, or None when the new
    state carries no severity (CONVALESCENT, NORMAL)."""


async def apply_state_transition(
    *,
    plan: StateTransitionPlan,
    db: AsyncSession,
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
) -> AppliedStateTransition:
    """Execute ``plan``'s I/O — write audit events, UPSERT risk_state.

    Step-by-step (audit-first, state-second per backend-spec §2.10.1):

    1. For each :class:`~services.risk.state_machine.PendingAuditEvent`
       in ``plan.audit_events`` (order is load-bearing —
       ``convalescent_counter_reset`` BEFORE ``state_transition_*`` when
       both fire), call :func:`append_audit_event`. The writer manages
       its own SERIALIZABLE transaction; each event commits independently.
       The LAST event in the iterable is the state-transition row whose
       UUID flows into the ``risk_state.audit_event_uuid`` linkage and
       the SSE envelope.
    2. UPSERT the ``risk_state`` row for ``account_id``: flip prior
       ``is_current = TRUE`` row to FALSE; INSERT a fresh row with the
       new state + the state-transition's event_uuid + ``is_current=TRUE``.
       The partial unique index ``risk_state_current`` on
       ``(account_id, is_current) WHERE is_current = TRUE`` enforces the
       one-row-per-account invariant; the two-step pattern is necessary
       because Postgres lacks ``ON CONFLICT (..) WHERE ..`` syntax for
       this shape.

    Returns
    -------
    :class:`AppliedStateTransition`
        With ``state_transition_audit_event_uuid`` set to the LAST audit
        write's UUID. The route layer reads this for the SSE envelope.

    Raises
    ------
    ValueError
        If ``plan.audit_events`` is empty (the policy layer should never
        produce a plan with no audit events; this is a defensive check
        to surface the bug rather than silently UPSERTing the state
        without an audit trail).
    services.audit.writer.AuditWriteFailure
        If :func:`append_audit_event` exhausts its 5-attempt SERIALIZABLE
        retry loop. Per backend-spec §2.10.1 the caller (the route
        handler) is responsible for translating this into HTTP 503 +
        triggering a secondary HALT via ``AUDIT_WRITE_FAIL``. Day 25
        does NOT wire that secondary halt — the operator's first
        observation is the 503; Day 26+ wires the secondary cascade.
    """
    if not plan.audit_events:
        raise ValueError(
            "apply_state_transition: plan.audit_events is empty; "
            "policy layer must emit at least one event per transition"
        )

    last_event_uuid: UUID | None = None
    for pending in plan.audit_events:
        record = await append_audit_event(
            db,
            pending.event_type,
            pending.payload,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
        )
        last_event_uuid = record.event_uuid

    # Guaranteed non-None by the if-check above + at least one append call.
    assert last_event_uuid is not None

    now = datetime.now(tz=UTC)
    async with db.begin():
        # Flip the prior is_current row to FALSE. NO-OP if no prior row
        # exists (first-ever transition for this account).
        await db.execute(
            text(
                "UPDATE risk_state SET is_current = FALSE "
                "WHERE account_id = :acc AND is_current = TRUE"
            ),
            {"acc": account_id},
        )
        # INSERT the new current row. vacation_active stays FALSE by default;
        # vacation transitions are an orthogonal write surface (Phase 1+).
        await db.execute(
            text(
                "INSERT INTO risk_state ("
                "    account_id, state, severity, reason, entered_at_utc, "
                "    convalescent_session_count, vacation_active, "
                "    audit_event_uuid, is_current"
                ") VALUES ("
                "    :acc, :state, :sev, :reason, :entered, "
                "    :counter, FALSE, "
                "    :audit_uuid, TRUE"
                ")"
            ),
            {
                "acc": account_id,
                "state": plan.new_state.value,
                "sev": plan.new_severity.value if plan.new_severity else None,
                "reason": plan.reason,
                "entered": now,
                "counter": plan.new_convalescent_counter,
                "audit_uuid": last_event_uuid,
            },
        )

    log.info(
        "risk_state_transition_applied",
        account_id=str(account_id),
        prior_state=plan.prior_state.value,
        new_state=plan.new_state.value,
        new_severity=plan.new_severity.value if plan.new_severity else None,
        reason=plan.reason,
        audit_event_uuid=str(last_event_uuid),
        audit_event_count=len(plan.audit_events),
    )

    # ``plan.new_state`` is a ``RiskState`` (StrEnum); mypy narrows ``.value``
    # to the locked literal automatically. Same for new_severity.
    return AppliedStateTransition(
        state_transition_audit_event_uuid=last_event_uuid,
        new_state=plan.new_state.value,
        new_severity=plan.new_severity.value if plan.new_severity else None,
    )


__all__ = [
    "AppliedStateTransition",
    "apply_state_transition",
]
