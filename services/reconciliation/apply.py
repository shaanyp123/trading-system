"""services/reconciliation/apply.py — I/O orchestrator for ReconciliationPlan.

# noqa-rationale: this module is the I/O complement to the pure-policy
# planner in `recon.py`. Schema mappings here are validated by the
# integration tests in `tests/integration/test_reconciliation_apply.py`.

Worker-PR-3b (post-pivot 2026-05-12). The pure-policy planner in
``services.reconciliation.recon.plan_reconciliation_check`` produces a
:class:`ReconciliationPlan` dataclass containing audit events to write,
breaks to insert, and prior breaks to mark resolved. This module takes
that plan and flushes it to the database.

**Audit-first ordering per backend-spec §2.10.1:**

  1. For each :class:`PendingAuditEvent` in ``plan.audit_events``: write
     via :func:`services.audit.writer.append_audit_event` in its own
     SERIALIZABLE transaction. Capture the resulting
     ``AuditLogRecord.event_uuid``.
  2. For each :class:`ReconciliationBreak` in ``plan.breaks_detected``:
     INSERT one row into ``reconciliation_breaks`` carrying the
     ``audit_event_uuid`` captured in step 1 (matched by the order the
     plan emits events).
  3. For each :class:`ResolvedPriorBreak` in ``plan.breaks_resolved``:
     UPDATE the prior ``reconciliation_breaks`` row's ``resolved_at_utc``
     + ``resolution_path``.

**Audit-event ↔ break matching:** the plan's ``audit_events`` tuple
emits ``reconciliation_break_detected`` events FIRST (one per break in
``breaks_detected``), then ``reconciliation_break_resolved`` events
(one per ``breaks_resolved``), then optionally a ``reconciliation_check_passed``
when no breaks were detected. We rely on this ordering to pair the
break dataclass with its audit_event_uuid via positional alignment.

**Kill-switch invocation:** when ``plan.should_invoke_kill_switch`` is
True (typically when an actionable break exists outside grace period),
the caller's ``state_transition_hook`` callback fires. The hook is
optional; if not provided the kill-switch transition is skipped (the
break audit row + ``reconciliation_breaks`` row still land). This
decouples the recon module from any specific state-machine wiring —
the hook is the seam.

**Alert dispatch:** for each :class:`AlertDescriptor` in ``plan.alerts``
(one per actionable break — grace-period continuations are skipped per
the planner contract), the caller's ``alert_dispatch_hook`` callback
fires. The hook receives the descriptor + the matching
``audit_event_uuid`` (resolved via positional alignment with
``plan.audit_events[: len(plan.breaks_detected)]``) and is responsible
for inserting the alerts row + invoking
``services.webhook_pusher.dispatcher.dispatch_alert``. The hook is
optional; if not provided, alerts are NOT dispatched (the audit chain +
reconciliation_breaks rows still land — so the operator can recover the
break via psql or the Audit page after wiring the hook). Same shape /
same A02 boundary as ``state_transition_hook``.

The alert dispatch hook fires AFTER audit writes + reconciliation_breaks
INSERTs + breaks_resolved UPDATEs (audit-first per backend-spec §2.10.1:
operator-visible side-effects come AFTER durable evidence). It fires
BEFORE the state_transition_hook so the operator gets the alert about
the break before the system halts.

**A02 BINDS** — `services/reconciliation/**` is on the forbidden
whitelist; `risk-review-approved` required.
**A01 enforced** — audit writes flow exclusively through
``append_audit_event``.
**A05 enforced** — Decimals throughout; no float arithmetic.
**A06 enforced** — every datetime tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.reconciliation.recon import (
    AlertDescriptor,
    PendingAuditEvent,
    ReconciliationBreak,
    ReconciliationPlan,
    ResolvedPriorBreak,
)

log = structlog.get_logger()


#: Map planner-emitted Literal strings → canonical AuditEventType enum.
#: The planner ships a Literal mirror for type-safety on the pure side;
#: the writer accepts both forms but we normalize here so structlog
#: logging shows the enum form consistently across modules.
_AUDIT_EVENT_TYPE_BY_STR: Final[dict[str, AuditEventType]] = {
    "reconciliation_check_passed": AuditEventType.RECONCILIATION_CHECK_PASSED,
    "reconciliation_break_detected": AuditEventType.RECONCILIATION_BREAK_DETECTED,
    "reconciliation_break_resolved": AuditEventType.RECONCILIATION_BREAK_RESOLVED,
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconciliationApplyResult:
    """Outcome of :func:`apply_reconciliation_plan`.

    Carries the audit-event UUIDs minted during the apply so downstream
    callers (kill-switch hook, structured-log aggregator) can correlate
    the apply with the audit row(s) it wrote.
    """

    audit_event_uuids: tuple[UUID, ...]
    """One UUID per ``plan.audit_events`` entry, in the same order."""

    inserted_break_ids: tuple[UUID, ...]
    """One UUID per inserted ``reconciliation_breaks`` row."""

    resolved_break_count: int
    """Count of prior breaks whose ``resolved_at_utc`` was stamped."""

    alerts_dispatched_count: int
    """Count of ``alert_dispatch_hook`` invocations. Equals
    ``len(plan.alerts)`` iff the hook was provided. Zero when the hook
    is None — alerts in the plan are then dropped (operator must
    rebuild from audit log; see ``deploy/reconciliation/README.md`` for
    the recovery procedure)."""

    kill_switch_invoked: bool
    """True if the state-transition hook fired."""


# ---------------------------------------------------------------------------
# Kill-switch hook contract
# ---------------------------------------------------------------------------


StateTransitionHook = Callable[["ReconciliationKillSwitchContext"], Awaitable[None]]
"""Callback fired when ``plan.should_invoke_kill_switch`` is True.

The hook is invoked AFTER the audit + reconciliation_breaks writes land
(audit-first per spec §2.10.1: the recon evidence is durable before any
state mutation). The hook implementer (the caller — typically the
scheduler) is responsible for constructing + applying a
``plan_invoke_kill_switch(trigger=RECON_MISMATCH, ...)`` plan.
"""


AlertDispatchHook = Callable[["AlertDispatchContext"], Awaitable[None]]
"""Callback fired once per :class:`AlertDescriptor` in ``plan.alerts``.

The hook receives the descriptor + the audit_event_uuid for the
triggering break (resolved by positional alignment in the apply
orchestrator) and is responsible for:

  1. INSERT one row into the ``alerts`` table with severity / category
     / message / detail / triggering_audit_event_uuid stamped from
     descriptor + context fields.
  2. Invoke
     :func:`services.webhook_pusher.dispatcher.dispatch_alert` with the
     newly minted alert_id + an httpx client + webhook URL map.

The hook is invoked AFTER audit + reconciliation_breaks writes land
(audit-first per spec §2.10.1). Errors raised inside the hook propagate
— the api-side wiring is expected to catch + log so a Discord outage
doesn't kill the EOD cycle.
"""


@dataclass(frozen=True, slots=True)
class ReconciliationKillSwitchContext:
    """Context payload passed to the state-transition hook.

    The hook reads these fields to build its ``plan_invoke_kill_switch``
    call. The recon-side detected_at_utc / actionable_break_count /
    representative audit_event_uuid flow through so the kill-switch audit
    payload can reference the recon evidence directly.
    """

    account_id: UUID
    env: Environment
    actionable_break_count: int
    primary_audit_event_uuid: UUID
    """The first ``reconciliation_break_detected`` audit_event_uuid from
    this apply. Carried in the kill-switch audit payload as the
    `triggered_by_audit_event_uuid` field for cross-reference."""


@dataclass(frozen=True, slots=True)
class AlertDispatchContext:
    """Context payload passed to the alert dispatch hook.

    The hook implementer translates these fields into an alerts row
    INSERT + a ``dispatch_alert`` call. Typical mapping:

      INSERT INTO alerts
        (account_id, severity, category, message, detail,
         triggering_audit_event_uuid)
      VALUES
        (account_id, descriptor.severity, descriptor.category,
         f"{descriptor.title}\\n\\n{descriptor.body}",
         descriptor.payload, triggering_audit_event_uuid)
      RETURNING id;
    """

    descriptor: AlertDescriptor
    """The pure-policy alert descriptor produced by the planner."""

    triggering_audit_event_uuid: UUID
    """The ``reconciliation_break_detected`` audit_event_uuid for the
    break that triggered this alert. Stamped into
    ``alerts.triggering_audit_event_uuid`` so the operator can
    cross-reference between the Audit page and the Alerts page."""

    account_id: UUID
    env: Environment


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


#: Resolution-path values accepted by the CHECK constraint on
#: `reconciliation_breaks.resolution_path` (alembic 0004, extended by the
#: 2026-07-13 ``recon_resolution_auto`` migration). The pure planner
#: emits a ``ResolvedPriorBreak`` for prior breaks that don't match
#: today's snapshot; the apply step's caller picks which path-string to
#: stamp. The EOD cycle passes ``auto_rereconciled`` (a subsequent recon
#: cycle observed the divergence gone — no human touched it); the
#: parameter default stays ``manual`` as the conservative value for any
#: operator-tooling caller that doesn't say otherwise.
ResolutionPathLiteral = Literal[
    "grace_period",
    "manual",
    "kill_switch",
    "tolerance_widened_dividend",
    "auto_rereconciled",
]


async def apply_reconciliation_plan(
    plan: ReconciliationPlan,
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit = 1,
    state_transition_hook: StateTransitionHook | None = None,
    alert_dispatch_hook: AlertDispatchHook | None = None,
    resolved_at_utc: datetime | None = None,
    resolution_path: ResolutionPathLiteral = "manual",
) -> ReconciliationApplyResult:
    """Flush a :class:`ReconciliationPlan` to the database.

    Audit-first ordering: writes ``plan.audit_events`` in declared order
    BEFORE any ``reconciliation_breaks`` row mutations. The break rows
    carry the audit_event_uuid FK so the chain stays single-source-of-
    truth.

    Pair-matching of audit events to breaks/resolutions is positional:

      ``plan.audit_events`` orders events as:
        ``[break_detected x N, break_resolved x M, optional check_passed]``

      where N == ``len(plan.breaks_detected)`` and
      M == ``len(plan.breaks_resolved)``. The orchestrator slices the
      resulting ``audit_event_uuids`` tuple the same way.

    Errors propagate. The audit-write retry mechanism is inside
    ``append_audit_event`` (5-attempt SQLSTATE-40001 retry per backend-
    spec §2.10.1); higher-order failures (SERIALIZATION_RETRY exhausted,
    DB unreachable) raise.

    Returns
    -------
    ReconciliationApplyResult
        - audit_event_uuids: minted UUIDs in declared order.
        - inserted_break_ids: PKs of new reconciliation_breaks rows.
        - resolved_break_count: count of UPDATE-resolved prior breaks.
        - kill_switch_invoked: True iff the state-transition hook fired.
    """
    audit_uuids: list[UUID] = []
    for pending in plan.audit_events:
        recorded = await _write_audit_event(
            pending,
            session_factory=session_factory,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
        )
        audit_uuids.append(recorded)

    # Positional slicing to pair audit_event_uuids with their breaks.
    n_detected = len(plan.breaks_detected)
    detected_uuids = audit_uuids[:n_detected]
    resolved_uuids = audit_uuids[n_detected : n_detected + len(plan.breaks_resolved)]

    inserted_break_ids: list[UUID] = []
    if plan.breaks_detected:
        inserted_break_ids = await _insert_breaks(
            plan.breaks_detected,
            audit_uuids=tuple(detected_uuids),
            account_id=account_id,
            session_factory=session_factory,
        )

    resolved_count = 0
    if plan.breaks_resolved:
        from datetime import UTC

        resolution_ts = resolved_at_utc or datetime.now(tz=UTC)
        resolved_count = await _resolve_prior_breaks(
            plan.breaks_resolved,
            audit_uuids=tuple(resolved_uuids),
            session_factory=session_factory,
            account_id=account_id,
            resolved_at_utc=resolution_ts,
            resolution_path=resolution_path,
        )

    alerts_dispatched_count = 0
    if plan.alerts and alert_dispatch_hook is not None:
        alerts_dispatched_count = await _dispatch_alerts(
            plan.alerts,
            audit_uuids=tuple(detected_uuids),
            account_id=account_id,
            env=env,
            alert_dispatch_hook=alert_dispatch_hook,
        )
    elif plan.alerts:
        # Hook not wired: log a single structured warning so the operator
        # knows alerts were dropped on the floor (the audit chain +
        # reconciliation_breaks rows still landed, so recovery is possible
        # via psql or the Audit page).
        log.warning(
            "reconciliation_alerts_dropped_no_hook",
            account_id=str(account_id),
            env=env,
            alerts_pending=len(plan.alerts),
            note=(
                "alert_dispatch_hook is None; alerts in plan.alerts will not "
                "be delivered. Wire the hook in the scheduler glue layer "
                "(see deploy/reconciliation/README.md)."
            ),
        )

    kill_switch_invoked = False
    if plan.should_invoke_kill_switch and state_transition_hook is not None:
        if not detected_uuids:
            # Defensive: kill-switch invocation without a detected break
            # would orphan the hook from the audit chain. The pure
            # planner only sets should_invoke_kill_switch when
            # actionable_break_count > 0, which implies at least one
            # detected break. We assert that invariant.
            log.error(
                "reconciliation_kill_switch_invariant_violated",
                actionable=plan.actionable_break_count,
                detected_count=n_detected,
            )
        else:
            await state_transition_hook(
                ReconciliationKillSwitchContext(
                    account_id=account_id,
                    env=env,
                    actionable_break_count=plan.actionable_break_count,
                    primary_audit_event_uuid=detected_uuids[0],
                )
            )
            kill_switch_invoked = True

    log.info(
        "reconciliation_plan_applied",
        account_id=str(account_id),
        env=env,
        audit_event_count=len(audit_uuids),
        breaks_detected=n_detected,
        breaks_resolved=resolved_count,
        actionable_break_count=plan.actionable_break_count,
        alerts_dispatched=alerts_dispatched_count,
        kill_switch_invoked=kill_switch_invoked,
    )
    return ReconciliationApplyResult(
        audit_event_uuids=tuple(audit_uuids),
        inserted_break_ids=tuple(inserted_break_ids),
        resolved_break_count=resolved_count,
        alerts_dispatched_count=alerts_dispatched_count,
        kill_switch_invoked=kill_switch_invoked,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _write_audit_event(
    pending: PendingAuditEvent,
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
) -> UUID:
    """Write one audit event in its own SERIALIZABLE transaction.

    The audit writer manages its own session lifecycle (advisory lock +
    SERIALIZABLE retry); we open a fresh session per event so we never
    interfere with the writer's transaction state. Matches the pattern
    from ``services.qc_adapter.signal_ingestion.ingest_signal_emitted``.
    """
    event_type = _AUDIT_EVENT_TYPE_BY_STR[pending.event_type]
    async with session_factory() as audit_session:
        record = await append_audit_event(
            audit_session,
            event_type,
            pending.payload,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
        )
    return record.event_uuid


async def _insert_breaks(
    breaks: tuple[ReconciliationBreak, ...],
    *,
    audit_uuids: tuple[UUID, ...],
    account_id: UUID,
    session_factory: async_sessionmaker[Any],
) -> list[UUID]:
    """INSERT one row per ReconciliationBreak; return generated PKs.

    Schema mapping (alembic 0004 reconciliation_breaks):
      * id (PK, generated)
      * account_id (FK → accounts)
      * detected_at_utc (TZ-aware)
      * metric / market / contract_id (contract_id resolved by caller —
        for Phase 1 we leave it NULL; the contracts.id lookup is a
        Phase 2 concern when the contract registry is wired)
      * expected / actual / delta / tolerance (Decimals)
      * source (TEXT — uses the BrokerSource enum value)
      * audit_event_uuid (FK-equivalent to the matched audit row)
      * within_grace_period → maps to resolution_path='grace_period'
        when True at insert time per backend-spec §2.6 grace semantics.
    """
    if len(breaks) != len(audit_uuids):
        raise ValueError(f"audit_uuids count {len(audit_uuids)} != breaks count {len(breaks)}")
    inserted_ids: list[UUID] = []
    async with session_factory() as session:
        async with session.begin():
            for break_, audit_uuid in zip(breaks, audit_uuids, strict=True):
                resolution_path = "grace_period" if break_.within_grace_period else None
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO reconciliation_breaks ("
                            "    account_id, detected_at_utc, metric, market,"
                            "    expected, actual, delta, tolerance,"
                            "    source, audit_event_uuid, resolution_path"
                            ") VALUES ("
                            "    :acct, :detected_at, :metric, :market,"
                            "    :expected, :actual, :delta, :tolerance,"
                            "    :source, :audit_uuid, :resolution_path"
                            ") RETURNING id"
                        ),
                        {
                            "acct": account_id,
                            "detected_at": break_.detected_at_utc,
                            "metric": break_.metric.value,
                            "market": break_.market,
                            "expected": break_.expected,
                            "actual": break_.actual,
                            "delta": break_.delta,
                            "tolerance": break_.tolerance,
                            "source": break_.source.value,
                            "audit_uuid": audit_uuid,
                            "resolution_path": resolution_path,
                        },
                    )
                ).fetchone()
                assert row is not None
                inserted_ids.append(UUID(str(row.id)))
    return inserted_ids


async def _dispatch_alerts(
    alerts: tuple[AlertDescriptor, ...],
    *,
    audit_uuids: tuple[UUID, ...],
    account_id: UUID,
    env: Environment,
    alert_dispatch_hook: AlertDispatchHook,
) -> int:
    """Fire ``alert_dispatch_hook`` once per :class:`AlertDescriptor`.

    Resolves each descriptor's triggering audit_event_uuid by positional
    alignment with ``audit_uuids`` (which is the detected-break-only
    slice of the apply's audit_uuids list — see the call site in
    :func:`apply_reconciliation_plan`).

    Hook invocation order matches descriptor order. Errors propagate;
    the api-side wiring is expected to catch + log Discord delivery
    failures via the dispatcher's own resilience path so a transient
    webhook outage doesn't kill the EOD cycle.

    Defensive on triggering_break_index out-of-bounds: if a malformed
    plan somehow points at a non-existent break index, log an error +
    skip the alert rather than crash (preserves audit row durability +
    surfaces the bug for the next deploy).
    """
    dispatched = 0
    for desc in alerts:
        if not 0 <= desc.triggering_break_index < len(audit_uuids):
            log.error(
                "reconciliation_alert_invariant_violated_index",
                triggering_break_index=desc.triggering_break_index,
                audit_uuids_len=len(audit_uuids),
                account_id=str(account_id),
                env=env,
            )
            continue
        triggering_uuid = audit_uuids[desc.triggering_break_index]
        await alert_dispatch_hook(
            AlertDispatchContext(
                descriptor=desc,
                triggering_audit_event_uuid=triggering_uuid,
                account_id=account_id,
                env=env,
            )
        )
        dispatched += 1
    return dispatched


async def _resolve_prior_breaks(
    resolved: tuple[ResolvedPriorBreak, ...],
    *,
    audit_uuids: tuple[UUID, ...],
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    resolved_at_utc: datetime,
    resolution_path: ResolutionPathLiteral,
) -> int:
    """UPDATE prior reconciliation_breaks rows: stamp resolved_at + path.

    Matching key: prior breaks are uniquely identified by
    (account_id, metric, market, delta) within the unresolved set. The
    ``account_id`` filter is load-bearing — without it, the UPDATE
    would clobber rows for other accounts (or for the same account
    across test runs that share a Postgres instance).

    The audit_uuids parameter is currently unused (the resolution audit
    write already lands before this method runs); kept in the signature
    for symmetry with :func:`_insert_breaks` and for a future iteration
    that links the resolution audit row back to the resolved break via
    an alembic migration adding `resolution_audit_event_uuid`.
    """
    if len(resolved) != len(audit_uuids):
        raise ValueError(f"audit_uuids count {len(audit_uuids)} != resolved count {len(resolved)}")
    if not resolved:
        return 0
    update_count = 0
    async with session_factory() as session:
        async with session.begin():
            for resolution in resolved:
                result = await session.execute(
                    text(
                        "UPDATE reconciliation_breaks SET "
                        "    resolved_at_utc = :resolved_at, "
                        "    resolution_path = :path "
                        "WHERE account_id = :acct "
                        "  AND metric = :metric "
                        "  AND COALESCE(market, '') = COALESCE(:market, '') "
                        "  AND delta = :delta "
                        "  AND resolved_at_utc IS NULL"
                    ),
                    {
                        "acct": account_id,
                        "resolved_at": resolved_at_utc,
                        "path": resolution_path,
                        "metric": resolution.metric.value,
                        "market": resolution.market,
                        "delta": resolution.delta,
                    },
                )
                update_count += result.rowcount or 0
    return update_count


__all__ = [
    "AlertDispatchContext",
    "AlertDispatchHook",
    "ReconciliationApplyResult",
    "ReconciliationKillSwitchContext",
    "ResolutionPathLiteral",
    "StateTransitionHook",
    "apply_reconciliation_plan",
]
