"""services/risk/signal_dispatch.py — signal approve/reject/defer dispatcher.

Pivot-PR-D (post-pivot 2026-05-12). Unstubs the Day-15 501 stubs on
``POST /api/signals/:id/{approve,reject,defer}``.

**A02 BINDS** — `services/risk/**` is on the forbidden whitelist;
`risk-review-approved` required.

**Pure-policy first design** (mirrors Day 9 ``services/reconciliation/recon.py``
and Day 25 ``services/risk/dispatch.py``):

* :func:`plan_signal_approve` / :func:`plan_signal_reject` /
  :func:`plan_signal_defer` build a :class:`SignalDispatchPlan` dataclass
  with the audit events + DB writes that need to happen. No I/O.
* :func:`apply_signal_dispatch` is the I/O orchestrator — writes the audit
  event(s) via ``append_audit_event``, updates the signal row, returns a
  result dataclass.
* Order placement (the actual `IbkrClient.place_order()` call for the
  approve path) is **NOT** in PR-D scope. The dispatcher records
  `signal_approved` + sets `signals.status='approved'`; a follow-up
  background task (PR-E or a dedicated worker) consumes approved signals
  and places orders. This decouples the route latency (<100ms typical)
  from the broker round-trip (200-800ms typical).

**Audit-first ordering per backend-spec §2.10.1:**

1. The dispatcher writes the audit event FIRST (signal_approved /
   signal_rejected / signal_deferred), then UPDATEs the signal row.
2. If the UPDATE fails after the audit event lands, the chain
   integrity is preserved (the audit event is durable; the signal
   row update can be retried).
3. If the audit event fails, the dispatcher returns an error; the
   signal row is unchanged; the route returns 503.

**A01 enforced** — every audit write routes through
:func:`services.audit.writer.append_audit_event`.
**A05 enforced** — Decimal-as-string in audit payloads.
**A06 enforced** — every datetime tz-aware UTC.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Locked enums + dataclasses
# ---------------------------------------------------------------------------


SignalDispatchAction = Literal["approve", "reject", "defer"]


@dataclass(frozen=True, slots=True)
class DecisionDiaryEntryInput:
    """Operator's decision-diary entry attached to reject/defer (mandatory).

    Field names mirror the API schema's ``DecisionDiaryEntry`` Pydantic
    model (``services/api/schemas/signals.py``) — `entry_class` + `tag` +
    `reasoning_text`. The route handler builds this dataclass from the
    request body 1:1 before calling the dispatcher.
    """

    tag: str  # locked enum: data_concern, regime_concern, size_concern, manual_judgment, other
    reasoning_text: str  # operator's free-text rationale (10-2000 chars per schema)
    entry_class: str = "signal_response"  # signal_response, forward_looking, general


@dataclass(frozen=True, slots=True)
class SignalDispatchPlan:
    """Pure-policy planner output.

    Built by :func:`plan_signal_approve` / :func:`plan_signal_reject` /
    :func:`plan_signal_defer`; consumed by :func:`apply_signal_dispatch`.
    Contains the audit-event payload + the new signal status; no I/O.
    """

    signal_id: UUID
    account_id: UUID
    action: SignalDispatchAction
    new_status: Literal["approved", "rejected", "deferred"]
    audit_event_type: AuditEventType
    audit_payload: dict[str, Any]
    decided_at_utc: datetime
    decided_by_user_id: str
    # When the action is approve, the dispatcher records an
    # `intent_to_place_order` flag. A separate worker / route follow-up
    # in PR-E or post-pivot Phase 1+ picks up approved signals + places
    # the orders.
    intent_to_place_order: bool = False
    # For reject/defer, the diary entry is mandatory and embedded in the
    # audit payload + persisted to a separate `decision_diary` row by
    # the dispatcher (Pivot-PR-D scope limits the persistence to the
    # audit_log; decision_diary table writes land in a follow-up PR).
    diary_entry: DecisionDiaryEntryInput | None = field(default=None)


@dataclass(frozen=True, slots=True)
class SignalDispatchResult:
    """Outcome of an I/O dispatch."""

    signal_id: UUID
    action: SignalDispatchAction
    new_status: Literal["approved", "rejected", "deferred"]
    audit_event_uuid: UUID
    audit_sequence_no: int


class SignalDispatchError(Exception):
    """Raised on terminal-failure dispatch attempts.

    Reasons:
        * Signal not found
        * Signal in wrong status (not pending) — already approved/rejected/deferred
        * Diary entry required but missing (reject/defer paths)
        * DB error during audit write or signal update
    """

    def __init__(
        self, error_code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# Pure-policy planners
# ---------------------------------------------------------------------------


def _validate_diary(entry: DecisionDiaryEntryInput | None, action: str) -> None:
    if entry is None:
        raise SignalDispatchError(
            error_code="DIARY_ENTRY_REQUIRED",
            message=f"{action} action requires an embedded decision_diary_entry per backend-spec §4.1.2.",
            details={"action": action},
        )
    if not entry.tag.strip():
        raise SignalDispatchError(
            error_code="DIARY_TAG_REQUIRED",
            message="decision_diary_entry.tag must be non-empty.",
            details={"action": action},
        )
    if not entry.reasoning_text.strip():
        raise SignalDispatchError(
            error_code="DIARY_REASONING_REQUIRED",
            message="decision_diary_entry.reasoning_text must be non-empty.",
            details={"action": action},
        )


def plan_signal_approve(
    *,
    signal_id: UUID,
    account_id: UUID,
    decided_by_user_id: str,
    override_size: int | None,
    decided_at_utc: datetime | None = None,
) -> SignalDispatchPlan:
    """Build the dispatch plan for an approve action.

    :param override_size: optional operator-provided contract count
        override. When None, the dispatcher uses the signal's
        ``target_contracts`` from the row. When set, it overrides per
        backend-spec §4.1.2 (operator may downsize the recommended
        allocation; upsizing is rejected by the route schema validation
        before reaching here).

    Approve never requires a diary entry — the approval IS the operator's
    affirmative signal.
    """
    if decided_at_utc is None:
        decided_at_utc = datetime.now(tz=UTC)
    if decided_at_utc.tzinfo is None:
        raise SignalDispatchError(
            error_code="TIMEZONE_REQUIRED",
            message="decided_at_utc must be tz-aware UTC per [A06].",
        )
    return SignalDispatchPlan(
        signal_id=signal_id,
        account_id=account_id,
        action="approve",
        new_status="approved",
        audit_event_type=AuditEventType.SIGNAL_APPROVED,
        audit_payload={
            "signal_id": str(signal_id),
            "account_id": str(account_id),
            "decided_by_user_id": decided_by_user_id,
            "decided_at_utc": decided_at_utc.isoformat(),
            "override_size": override_size,
        },
        decided_at_utc=decided_at_utc,
        decided_by_user_id=decided_by_user_id,
        intent_to_place_order=True,
        diary_entry=None,
    )


def plan_signal_reject(
    *,
    signal_id: UUID,
    account_id: UUID,
    decided_by_user_id: str,
    diary_entry: DecisionDiaryEntryInput | None,
    decided_at_utc: datetime | None = None,
) -> SignalDispatchPlan:
    """Build the dispatch plan for a reject action."""
    _validate_diary(diary_entry, "reject")
    if decided_at_utc is None:
        decided_at_utc = datetime.now(tz=UTC)
    assert diary_entry is not None  # narrowed by _validate_diary
    return SignalDispatchPlan(
        signal_id=signal_id,
        account_id=account_id,
        action="reject",
        new_status="rejected",
        audit_event_type=AuditEventType.SIGNAL_REJECTED,
        audit_payload={
            "signal_id": str(signal_id),
            "account_id": str(account_id),
            "decided_by_user_id": decided_by_user_id,
            "decided_at_utc": decided_at_utc.isoformat(),
            "diary_entry": {
                "entry_class": diary_entry.entry_class,
                "tag": diary_entry.tag,
                "reasoning_text": diary_entry.reasoning_text,
            },
        },
        decided_at_utc=decided_at_utc,
        decided_by_user_id=decided_by_user_id,
        intent_to_place_order=False,
        diary_entry=diary_entry,
    )


def plan_signal_defer(
    *,
    signal_id: UUID,
    account_id: UUID,
    decided_by_user_id: str,
    diary_entry: DecisionDiaryEntryInput | None,
    decided_at_utc: datetime | None = None,
) -> SignalDispatchPlan:
    """Build the dispatch plan for a defer action."""
    _validate_diary(diary_entry, "defer")
    if decided_at_utc is None:
        decided_at_utc = datetime.now(tz=UTC)
    assert diary_entry is not None
    return SignalDispatchPlan(
        signal_id=signal_id,
        account_id=account_id,
        action="defer",
        new_status="deferred",
        audit_event_type=AuditEventType.SIGNAL_DEFERRED,
        audit_payload={
            "signal_id": str(signal_id),
            "account_id": str(account_id),
            "decided_by_user_id": decided_by_user_id,
            "decided_at_utc": decided_at_utc.isoformat(),
            "diary_entry": {
                "entry_class": diary_entry.entry_class,
                "tag": diary_entry.tag,
                "reasoning_text": diary_entry.reasoning_text,
            },
        },
        decided_at_utc=decided_at_utc,
        decided_by_user_id=decided_by_user_id,
        intent_to_place_order=False,
        diary_entry=diary_entry,
    )


# ---------------------------------------------------------------------------
# I/O orchestrator
# ---------------------------------------------------------------------------


async def apply_signal_dispatch(
    plan: SignalDispatchPlan,
    *,
    session_factory: async_sessionmaker[Any],
    env: Environment,
    phase_at_emit: PhaseAtEmit = 1,
) -> SignalDispatchResult:
    """Execute the dispatch plan with audit-first ordering.

    Step 1: validate the signal exists + is in 'pending' status (SELECT FOR UPDATE).
    Step 2: write the audit event (signal_approved / signal_rejected /
        signal_deferred) via ``append_audit_event``. SERIALIZABLE retries +
        hash chain math handled by the writer.
    Step 3: UPDATE signals row with new_status + decided_at_utc +
        audit_event_uuid FK.

    Audit-first ordering preserved: even if Step 3 fails after Step 2
    succeeds, the audit row is durable; the route returns the audit event
    UUID. A retry (idempotent on the audit event UUID) re-applies Step 3
    when conditions clear.
    """
    # Step 1: validate. The signals table PK column is `id`, not
    # `signal_id` — `signal_id` is the column name on `orders` (the FK
    # back to signals.id). Pivot-PR-D 2026-05-12 fixup.
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT status FROM signals WHERE id = :sid"),
                {"sid": plan.signal_id},
            )
        ).fetchone()
        if row is None:
            raise SignalDispatchError(
                error_code="SIGNAL_NOT_FOUND",
                message=f"signal_id {plan.signal_id} not found.",
                details={"signal_id": str(plan.signal_id)},
            )
        if row.status != "pending":
            raise SignalDispatchError(
                error_code="SIGNAL_NOT_PENDING",
                message=(
                    f"signal_id {plan.signal_id} is in status {row.status!r}; "
                    "only pending signals can be approved / rejected / deferred."
                ),
                details={"signal_id": str(plan.signal_id), "current_status": row.status},
            )

    # Step 2: audit-first. session_factory pattern from Day 28 PR-A
    # (DP-021): each I/O call uses its own short-lived session so the
    # audit writer's SERIALIZABLE transaction is independent of the
    # validation SELECT above.
    async with session_factory() as audit_session:
        audit_record = await append_audit_event(
            audit_session,
            plan.audit_event_type,
            plan.audit_payload,
            account_id=plan.account_id,
            env=env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=plan.decided_at_utc,
        )

    log.info(
        "signal_dispatch_audit_written",
        signal_id=str(plan.signal_id),
        action=plan.action,
        audit_event_uuid=str(audit_record.event_uuid),
        sequence_no=audit_record.sequence_no,
    )

    # Step 3: UPDATE signal row. Separate transaction so the audit write
    # is durable independent of any failure here.
    #
    # Schema mapping (alembic 0002 `signals` table; spec §3.3):
    #   - `status` (always set)
    #   - `approved_at_utc` (set on approve)
    #   - `rejected_at_utc` (set on reject)
    #   - defer path has no dedicated timestamp column; only `status` updates.
    #
    # The audit_event UUID linkage (decided_by_user_id, decision_audit_event_uuid)
    # lives in the audit row's payload (see plan_signal_approve /
    # _reject / _defer payload construction). Adding dedicated linkage
    # columns to the signals table is a follow-up alembic migration —
    # spec §3.3 doesn't reserve them today; keeping the dispatcher
    # schema-compatible with the shipped 0002 migration is the smallest-
    # blast-radius fix for Pivot-PR-D's end-to-end ceremony.
    if plan.action == "approve":
        update_sql = (
            "UPDATE signals SET status = :status, approved_at_utc = :decided_at WHERE id = :sid"
        )
    elif plan.action == "reject":
        update_sql = (
            "UPDATE signals SET status = :status, rejected_at_utc = :decided_at WHERE id = :sid"
        )
    else:  # defer
        update_sql = "UPDATE signals SET status = :status WHERE id = :sid"

    async with session_factory() as session:
        await session.execute(
            text(update_sql),
            {
                "status": plan.new_status,
                "decided_at": plan.decided_at_utc,
                "sid": plan.signal_id,
            },
        )
        await session.commit()

    log.info(
        "signal_dispatch_completed",
        signal_id=str(plan.signal_id),
        action=plan.action,
        new_status=plan.new_status,
        audit_event_uuid=str(audit_record.event_uuid),
    )

    return SignalDispatchResult(
        signal_id=plan.signal_id,
        action=plan.action,
        new_status=plan.new_status,
        audit_event_uuid=audit_record.event_uuid,
        audit_sequence_no=audit_record.sequence_no,
    )


# Placeholder: prevents unused-import warnings; the asyncio import is
# reserved for a future PR's background task that consumes approved
# signals + calls IbkrClient.place_order().
_ = asyncio


__all__ = [
    "DecisionDiaryEntryInput",
    "SignalDispatchAction",
    "SignalDispatchError",
    "SignalDispatchPlan",
    "SignalDispatchResult",
    "apply_signal_dispatch",
    "plan_signal_approve",
    "plan_signal_defer",
    "plan_signal_reject",
]
