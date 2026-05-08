"""Vacation mode handler — pure-policy plan for start/end (backend-spec §3.18).

Same plan-then-apply shape as services/risk/sizing.py and services/risk/
state_machine.py: returns ``VacationPlan`` structs with audit_events +
sse_event as data; the caller (``services/api/routes/system.py`` when it
lands) owns the I/O — UPDATE ``vacation_mode`` + ``risk_state``, append
audit events, emit SSE.

Backend-spec contract:
- ``vacation_mode`` table (§3.18) columns: account_id, started_at_utc,
  scheduled_end_utc, ended_at_utc, ended_by, audit_event_uuid_start,
  audit_event_uuid_end.
- ``risk_state`` table (§3.14) carries ``vacation_active BOOLEAN`` and
  ``vacation_until_utc TIMESTAMPTZ`` so connected SSE clients can see the
  state without joining tables.
- Audit event types from the locked taxonomy (§3.30):
  ``vacation_started``, ``vacation_ended``. The implementation-guide.md
  Day 9 prompt mentions ``vacation_mode_toggled`` — that name is NOT in
  the locked taxonomy. Per anti-pattern A04, this module emits the
  spec-canonical names instead.

Constraints:
- ``start_vacation(days)``: 1 <= days <= 30. Anything outside raises
  ``VacationError`` (returns 400 to the client).
- ``end_vacation``: requires a web-only operator session (re-auth gate;
  enforced at the API layer, not here). The ``ended_by`` column accepts
  ``operator_command`` (Discord) or ``operator_web``; only the latter
  short-circuits an active vacation per spec §3.18 + dev-guide §1.5
  ("re-auth window: web-only by construction").

Tests in ``tests/unit/test_vacation.py`` cover:
- start_vacation max 30 days enforcement (boundary 30, 31)
- start_vacation min 1 day enforcement (0, negative)
- end_vacation requires web session (Discord path rejected at policy
  layer for clarity even though the API gate is the primary enforcer)
- Audit + SSE payload shape
- end_vacation when no active vacation raises
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID


class VacationEndedBy(StrEnum):
    EXPIRY = "expiry"
    OPERATOR_COMMAND = "operator_command"  # Discord (locked: cannot end via Discord)
    OPERATOR_WEB = "operator_web"  # web with re-auth (the only manual end path)


#: Maximum vacation duration in days. Backend-spec §2.9 + implementation-guide
#: §11 Day 9 both reference 30-day max. Anything longer requires multiple
#: explicit re-confirmations.
VACATION_MAX_DAYS: Final[int] = 30
VACATION_MIN_DAYS: Final[int] = 1

# Locked-taxonomy event types (backend-spec §3.30).
VacationAuditEventType = Literal["vacation_started", "vacation_ended"]


class VacationError(ValueError):
    """Raised on invalid vacation start/end requests.

    ValueError-subclass so FastAPI handlers can trap with a single branch
    and return a 400 with the canonical error envelope (dev-guide §3.6).
    """


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """An audit event the caller writes via ``append_audit_event()``.

    Same shape as services.risk.sizing.PendingAuditEvent and services.risk.
    state_machine.PendingAuditEvent — single flush helper across modules.
    """

    event_type: VacationAuditEventType
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PendingSSEEvent:
    """SSE event for the caller to emit. Backend-spec §4.2.2 ``risk_state``."""

    event_type: Literal["risk_state"]
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StartVacationPlan:
    """Plan for ``start_vacation`` to be applied by the caller.

    Caller:
      1. INSERT vacation_mode (account_id, started_at_utc=now,
         scheduled_end_utc, audit_event_uuid_start=<from audit write>,
         ended_at_utc=NULL, ended_by=NULL).
      2. UPDATE risk_state SET vacation_active=TRUE,
         vacation_until_utc=scheduled_end_utc.
      3. append_audit_event(vacation_started, payload) — fills
         audit_event_uuid_start above.
      4. emit_sse(risk_state, sse_event.data).
    """

    account_id: UUID
    started_at_utc: datetime
    scheduled_end_utc: datetime
    days: int
    audit_event: PendingAuditEvent
    sse_event: PendingSSEEvent


@dataclass(frozen=True, slots=True)
class EndVacationPlan:
    """Plan for ``end_vacation`` to be applied by the caller.

    Caller:
      1. UPDATE vacation_mode (current row) SET ended_at_utc=now,
         ended_by=ended_by, audit_event_uuid_end=<from audit write>.
      2. UPDATE risk_state SET vacation_active=FALSE,
         vacation_until_utc=NULL.
      3. append_audit_event(vacation_ended, payload).
      4. emit_sse(risk_state, sse_event.data).
    """

    account_id: UUID
    ended_at_utc: datetime
    ended_by: VacationEndedBy
    audit_event: PendingAuditEvent
    sse_event: PendingSSEEvent


def plan_start_vacation(
    *,
    account_id: UUID,
    days: int,
    operator_session_id: str,
    started_at_utc: datetime | None = None,
) -> StartVacationPlan:
    """Validate and build a start-vacation plan.

    Raises ``VacationError`` on out-of-range ``days``. Caller MUST also
    verify there is no active vacation row already (single-row-per-account
    invariant; spec §3.18 partial unique index ``vacation_active``).
    """
    if days < VACATION_MIN_DAYS:
        raise VacationError(
            f"days must be >= {VACATION_MIN_DAYS}; got {days} "
            f"(vacation duration cannot be zero or negative)"
        )
    if days > VACATION_MAX_DAYS:
        raise VacationError(
            f"days must be <= {VACATION_MAX_DAYS}; got {days} (spec §2.9 max vacation duration)"
        )

    start = started_at_utc if started_at_utc is not None else datetime.now(tz=UTC)
    end = start + timedelta(days=days)

    audit_event = PendingAuditEvent(
        event_type="vacation_started",
        payload={
            "account_id": str(account_id),
            "started_at_utc": start.isoformat(),
            "scheduled_end_utc": end.isoformat(),
            "days": days,
            "operator_session_id": operator_session_id,
        },
    )
    sse_event = PendingSSEEvent(
        event_type="risk_state",
        data={
            "vacation_active": True,
            "vacation_until_utc": end.isoformat(),
            "audit_event_uuid": None,  # filled by caller post-audit-write
        },
    )

    return StartVacationPlan(
        account_id=account_id,
        started_at_utc=start,
        scheduled_end_utc=end,
        days=days,
        audit_event=audit_event,
        sse_event=sse_event,
    )


def plan_end_vacation(
    *,
    account_id: UUID,
    ended_by: VacationEndedBy,
    operator_session_id: str | None,
    has_active_vacation: bool,
    ended_at_utc: datetime | None = None,
) -> EndVacationPlan:
    """Validate and build an end-vacation plan.

    Manual ends MUST come from a web session (``ended_by=operator_web``);
    Discord-path ends are rejected. The dev-guide §1.5 lock states "re-auth
    window: web-only by construction" — applying that to vacation end here
    enforces the rule at the policy layer in addition to the API gate.

    The ``expiry`` path (cron-driven natural end) bypasses the web-only
    rule (no operator session involved).

    ``has_active_vacation``: caller queries ``vacation_mode`` for an open
    row before calling this. False raises a ``VacationError`` (no-op end
    is treated as a caller bug at the policy layer; the API can interpret
    that as 404).
    """
    if not has_active_vacation:
        raise VacationError("no active vacation to end (vacation_mode has no open row for account)")
    if ended_by == VacationEndedBy.OPERATOR_COMMAND:
        raise VacationError(
            "vacation cannot be ended via Discord (operator_command); "
            "manual end requires re-auth on the web (dev-guide §1.5 web-only rule)"
        )
    if ended_by == VacationEndedBy.OPERATOR_WEB and not operator_session_id:
        raise VacationError(
            "operator_session_id is required for ended_by=operator_web (re-auth proof)"
        )

    end_ts = ended_at_utc if ended_at_utc is not None else datetime.now(tz=UTC)

    payload: dict[str, Any] = {
        "account_id": str(account_id),
        "ended_at_utc": end_ts.isoformat(),
        "ended_by": ended_by.value,
    }
    if operator_session_id is not None:
        payload["operator_session_id"] = operator_session_id

    audit_event = PendingAuditEvent(event_type="vacation_ended", payload=payload)
    sse_event = PendingSSEEvent(
        event_type="risk_state",
        data={
            "vacation_active": False,
            "vacation_until_utc": None,
            "ended_by": ended_by.value,
            "audit_event_uuid": None,
        },
    )

    return EndVacationPlan(
        account_id=account_id,
        ended_at_utc=end_ts,
        ended_by=ended_by,
        audit_event=audit_event,
        sse_event=sse_event,
    )


__all__ = [
    "VACATION_MAX_DAYS",
    "VACATION_MIN_DAYS",
    "EndVacationPlan",
    "PendingAuditEvent",
    "PendingSSEEvent",
    "StartVacationPlan",
    "VacationAuditEventType",
    "VacationEndedBy",
    "VacationError",
    "plan_end_vacation",
    "plan_start_vacation",
]
