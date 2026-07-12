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
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.risk.state_machine import (
    RiskState,
    StateTransitionPlan,
    clean_day_skip_reason,
    plan_session_close,
)

log = structlog.get_logger()


class StaleRiskStateError(Exception):
    """The risk_state row changed between the caller's read and the apply.

    Raised only when the caller opted into the conditional flip via
    ``apply_state_transition(expected_prior_state=...)`` and the current
    row's ``state`` no longer matches — e.g. an auto-halt landed while a
    risk-LOOSENING route (false-positive adjudication, PR #376) was
    between its read and its write. The state transaction aborts BEFORE
    the INSERT, so the concurrent transition's row stays current.

    Residual by design: the plan's audit events were already committed
    (audit-first per §2.10.1), leaving an "attempted" record with no
    matching risk_state row — the same self-healing forensics shape the
    convalescent clean-day tick documents for its FOR-UPDATE guard.
    Callers translate this to 409 and the operator simply re-inspects.
    """

    def __init__(self, *, expected: str, account_id: UUID) -> None:
        self.expected = expected
        self.account_id = account_id
        super().__init__(
            f"risk_state for account {account_id} is no longer '{expected}' "
            "(concurrent transition won); aborting before INSERT"
        )


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
    expected_prior_state: RiskState | None = None,
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

    ``expected_prior_state`` (opt-in, PR #376 race guard): when set, the
    flip statement additionally requires the current row's ``state`` to
    still equal it — closing the read→apply TOCTOU window for
    risk-LOOSENING callers. If a concurrent transition changed the state
    first (e.g. an auto-halt landed mid-adjudication), the flip matches
    0 rows and :class:`StaleRiskStateError` raises BEFORE the INSERT —
    the concurrent (risk-tightening) transition wins, never the loosener.
    ``None`` (the default) preserves the historical unconditional flip
    for all existing callers.

    Raises
    ------
    ValueError
        If ``plan.audit_events`` is empty (the policy layer should never
        produce a plan with no audit events; this is a defensive check
        to surface the bug rather than silently UPSERTing the state
        without an audit trail).
    StaleRiskStateError
        If ``expected_prior_state`` is set and the current row's state
        no longer matches (see above). The audit events are already
        committed (audit-first) — an "attempted" forensic record with
        no state change; the route translates to 409.
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
        # exists (first-ever transition for this account). With
        # expected_prior_state set, the flip is CONDITIONAL on the state
        # still matching — rowcount 0 means a concurrent transition won
        # the race; abort before the INSERT so its row stays current.
        if expected_prior_state is not None:
            # Runtime object for DML is a CursorResult (has rowcount);
            # AsyncSession.execute is typed as the Result base.
            flip = cast(
                CursorResult[Any],
                await db.execute(
                    text(
                        "UPDATE risk_state SET is_current = FALSE "
                        "WHERE account_id = :acc AND is_current = TRUE "
                        "  AND state = :expected"
                    ),
                    {"acc": account_id, "expected": expected_prior_state.value},
                ),
            )
            if flip.rowcount != 1:
                raise StaleRiskStateError(
                    expected=expected_prior_state.value, account_id=account_id
                )
        else:
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


async def apply_convalescent_clean_day_tick(
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
    now_utc: datetime,
) -> AppliedStateTransition | None:
    """Count one clean UTC day toward CONVALESCENT graduation, if due.

    The production caller is the 00:15 UTC recon EOD cycle (via the api
    lifespan's convalescent-tick hook): the tick running on UTC day T
    evaluates day T-1 against :func:`~services.risk.state_machine.
    clean_day_skip_reason` (resume day counts, breach day never counts,
    once per day — the 2026-07-09 operator amendment; 3 clean days
    graduate).

    Write shapes (policy in ``state_machine``, I/O here, per the module
    docstring):

    * **Non-graduating day** — a plain UPDATE of
      ``convalescent_session_count`` + the ``convalescent_last_counted_
      day_utc`` marker in ONE transaction. Deliberately NO audit row:
      the locked taxonomy has no per-tick event type (documented
      deferral in ``plan_session_close``); the graduation event carries
      ``convalescent_sessions_completed`` so the chain still shows the
      journey. Returns None.
    * **Graduating day** — audit-first per backend-spec §2.10.1: the
      ``state_transition_convalescent_to_normal`` event is appended (via
      :func:`append_audit_event`, its own session/transaction) BEFORE
      the ``risk_state`` flip+INSERT, mirroring
      :func:`apply_state_transition`'s two-step shape. Returns the
      :class:`AppliedStateTransition` so the caller can fan out SSE.

    Locking: the current row is read ``FOR UPDATE`` and held across the
    decision + write, so a concurrent auto-halt (risk loop / recon
    kill-switch hook, which rewrites the same row) serializes against
    the tick instead of racing it. The audit write happens on a separate
    session while the lock is held — different tables (audit_log vs
    risk_state), so no lock-order inversion is possible. What the lock
    buys, precisely (2026-07-09 risk review): if the HALT wins the lock,
    the tick re-reads and skips (fail-safe); if the TICK wins, a halt
    blocked behind it fails LOUDLY on the ``risk_state_current`` partial
    unique index and the worker's next 30 s risk tick re-fires it —
    fail-loud-with-retry rather than a silent stomp. It does NOT make
    the pair atomic. (Follow-up resolved 2026-07-12: the worker's
    ``_tick_failure_halt_fired`` latch now sets only after
    ``invoke_kill_switch`` returns, so a raised invoke retries on the
    next 30 s tick instead of being swallowed.)

    Failure forensics: if the ``risk_state`` write fails AFTER the
    graduation audit event committed (the documented audit-canonical
    direction), the row stays CONVALESCENT with the marker unset and the
    NEXT day's tick re-runs graduation — the chain may then carry TWO
    ``state_transition_convalescent_to_normal`` events for one
    graduation. Self-healing, not corruption; read the risk_state row's
    ``audit_event_uuid`` linkage to identify the effective one.

    Fail-safe by construction: every early return leaves the counter
    untouched; a skipped or failed tick can only DELAY graduation,
    never accelerate it.
    """
    if now_utc.tzinfo is None:
        raise ValueError(f"now_utc must be tz-aware (dev-guide §3 / A06); got naive {now_utc!r}")
    counted_day = now_utc.astimezone(UTC).date() - timedelta(days=1)

    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT id, state, convalescent_session_count, entered_at_utc, "
                        "       convalescent_last_counted_day_utc "
                        "FROM risk_state "
                        "WHERE account_id = :acc AND is_current = TRUE "
                        "FOR UPDATE"
                    ),
                    {"acc": account_id},
                )
            ).fetchone()
            if row is None:
                log.info(
                    "convalescent_tick_skipped",
                    account_id=str(account_id),
                    reason="no_current_risk_state_row",
                    counted_day=counted_day.isoformat(),
                )
                return None

            # Breach-day guard input: the most recent HALT_NEW row is
            # necessarily the halt preceding the current CONVALESCENT
            # stint (a newer halt would make the CURRENT state HALT_NEW,
            # failing the predicate anyway).
            halt_row = (
                await session.execute(
                    text(
                        "SELECT entered_at_utc FROM risk_state "
                        "WHERE account_id = :acc AND state = 'HALT_NEW' "
                        "ORDER BY entered_at_utc DESC LIMIT 1"
                    ),
                    {"acc": account_id},
                )
            ).fetchone()
            halt_day = (
                halt_row.entered_at_utc.astimezone(UTC).date() if halt_row is not None else None
            )

            entered_at = row.entered_at_utc
            if entered_at.tzinfo is None:  # asyncpg returns tz-aware; defensive
                entered_at = entered_at.replace(tzinfo=UTC)
            skip_reason = clean_day_skip_reason(
                current_state=RiskState(row.state),
                entered_at_utc=entered_at,
                last_counted_day_utc=row.convalescent_last_counted_day_utc,
                halt_day_utc=halt_day,
                counted_day_utc=counted_day,
            )
            if skip_reason is not None:
                log.info(
                    "convalescent_tick_skipped",
                    account_id=str(account_id),
                    reason=skip_reason,
                    state=row.state,
                    counted_day=counted_day.isoformat(),
                )
                return None

            counter_before = int(row.convalescent_session_count or 0)
            plan = plan_session_close(
                current_state=RiskState.CONVALESCENT,
                convalescent_counter=counter_before,
                timestamp_utc=now_utc.isoformat(),
            )

            if plan is None:
                await session.execute(
                    text(
                        "UPDATE risk_state SET "
                        "    convalescent_session_count = :counter, "
                        "    convalescent_last_counted_day_utc = :day "
                        "WHERE id = :row_id"
                    ),
                    {"counter": counter_before + 1, "day": counted_day, "row_id": row.id},
                )
                log.info(
                    "convalescent_clean_day_counted",
                    account_id=str(account_id),
                    counted_day=counted_day.isoformat(),
                    counter_before=counter_before,
                    counter_after=counter_before + 1,
                )
                return None

            # Graduation: audit-first on a SEPARATE session while the row
            # lock is held (see docstring), then the flip+INSERT.
            last_event_uuid: UUID | None = None
            async with session_factory() as audit_session:
                for pending in plan.audit_events:
                    record = await append_audit_event(
                        audit_session,
                        pending.event_type,
                        pending.payload,
                        account_id=account_id,
                        env=env,
                        phase_at_emit=phase_at_emit,
                    )
                    last_event_uuid = record.event_uuid
            assert last_event_uuid is not None  # plan always carries the transition event

            await session.execute(
                text("UPDATE risk_state SET is_current = FALSE WHERE id = :row_id"),
                {"row_id": row.id},
            )
            await session.execute(
                text(
                    "INSERT INTO risk_state ("
                    "    account_id, state, severity, reason, entered_at_utc, "
                    "    convalescent_session_count, vacation_active, "
                    "    audit_event_uuid, is_current"
                    ") VALUES ("
                    "    :acc, :state, NULL, :reason, :entered, "
                    "    :counter, FALSE, :audit_uuid, TRUE"
                    ")"
                ),
                {
                    "acc": account_id,
                    "state": plan.new_state.value,
                    "reason": plan.reason,
                    "entered": now_utc,
                    "counter": plan.new_convalescent_counter,
                    "audit_uuid": last_event_uuid,
                },
            )

    log.info(
        "convalescent_graduated",
        account_id=str(account_id),
        counted_day=counted_day.isoformat(),
        sessions_completed=counter_before + 1,
        audit_event_uuid=str(last_event_uuid),
    )
    return AppliedStateTransition(
        state_transition_audit_event_uuid=last_event_uuid,
        new_state=plan.new_state.value,
        new_severity=None,
    )


__all__ = [
    "AppliedStateTransition",
    "StaleRiskStateError",
    "apply_convalescent_clean_day_tick",
    "apply_state_transition",
]
