"""services/api/routes/system.py — `/api/system/*` Phase-1 endpoints (subset).

Day 15 (Week 5 Mon) shipped:

  * ``GET  /api/system/status`` — composite snapshot for the Today tile.
  * ``GET  /api/system/kill-switch`` — narrow projection of the kill-switch
    state (a strict subset of /system/status; called by the dedicated
    kill-switch UI tile).
  * ``POST /api/system/kill-switch/invoke`` — body-validated 501 stub.
  * ``POST /api/system/kill-switch/resume`` — body-validated 501 stub.

Day 25 (Week 7 Mon) unstubs the two POST endpoints end-to-end:

  * **invoke** — looks up the current ``risk_state``, plans the
    NORMAL/CONVALESCENT → HALT_NEW transition via
    :func:`services.risk.state_machine.plan_invoke_kill_switch`, applies
    the plan (writes audit events + UPSERTs ``risk_state``) via
    :func:`services.risk.dispatch.apply_state_transition`, then emits the
    canonical ``risk_state`` SSE envelope.
  * **resume** — same shape against
    :func:`~services.risk.state_machine.plan_resume_from_halt`, with the
    5-minute WebAuthn re-auth gate
    (``services.api.auth.sessions.require_recent_uv``) per dev-guide §1.5
    LOCKED.

Conflict handling:

  * ``invoke`` while ``risk_state=HALT_NEW`` returns 409
    ``ALREADY_HALTED`` — the policy layer rejects HALT_NEW → HALT_NEW; the
    route surfaces a friendlier error than the policy's
    :class:`IllegalTransitionError`.
  * ``resume`` while ``risk_state != HALT_NEW`` returns 409
    ``NOT_HALTED``.

NOT in scope today (deferred to Week 5 Tue-Fri or later sessions):

  * ``GET /api/system/risk-envelope`` (Phase 1 readonly view)
  * ``POST /api/system/vacation/{start,end}``
  * ``GET /api/system/audit*``
  * ``GET /api/system/deployments`` / rollback (Phase 2)
  * ``GET /api/system/agent-activity`` (Phase 2)
  * ``GET /api/system/costs``, ``GET /api/system/watchdog``,
    ``POST /api/internal/watchdog`` (Week 5 Wed Caddy bringup or later).

The ``server_now`` field is computed at response build time per spec
§4.1.6. The ``backend_version`` echoes ``APISettings.version``;
``expected_frontend_version`` is hardcoded to the same value in Phase 0
(no version-skew enforcement until Week 6 frontend ships).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.auth import sessions as sessions_mod
from services.api.config import APISettings, get_settings
from services.api.db import get_session
from services.api.errors import AppError
from services.api.repos.phase1 import (
    Phase1QueryRepo,
    PostgresPhase1QueryRepo,
    RiskStateRow,
)
from services.api.schemas.common import EPOCH_SENTINEL_UTC
from services.api.schemas.system import (
    KillSwitchInvokeRequest,
    KillSwitchResumeRequest,
    KillSwitchStatus,
    KillSwitchTransitionResponse,
    ReconciliationSummary,
    SystemStatus,
)
from services.api.session import SessionContext, get_session_context
from services.api.sse import emit_sse
from services.audit.writer import Environment
from services.risk.dispatch import apply_state_transition
from services.risk.state_machine import (
    HaltSeverity,
    IllegalTransitionError,
    RiskState,
    TransitionTrigger,
    plan_invoke_kill_switch,
    plan_resume_from_halt,
)

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


def _default_risk_state(now: datetime) -> RiskStateRow:
    """Return a synthetic NORMAL risk-state row when the table has no current row.

    Phase 0 reality: the ``risk_state`` table has no row until the first
    transition runs ``apply_state_transition``. Rather than 500 on a missing
    row, we synthesize a NORMAL state with neutral counters. The synthetic
    row's ``audit_event_uuid`` is the all-zero UUID — a sentinel readers
    can recognise as "no real transition has been recorded".
    """
    return RiskStateRow(
        state="NORMAL",
        severity=None,
        reason=None,
        entered_at_utc=now,
        convalescent_session_count=0,
        vacation_active=False,
        vacation_until_utc=None,
        audit_event_uuid=UUID(int=0),
    )


async def _load_status_components(
    repo: Phase1QueryRepo,
    account_id: UUID | None,
    now: datetime,
) -> tuple[RiskStateRow, ReconciliationSummary, datetime]:
    """Pull risk_state + recon summary + watchdog ping in three queries.

    When ``account_id`` is None (Phase 0, no operator yet), every component
    falls back to its empty-data shape: NORMAL state, recon summary with
    epoch sentinel + zero counters, watchdog ping = epoch sentinel.
    """
    if account_id is None:
        return (
            _default_risk_state(now),
            ReconciliationSummary(
                last_check_utc=EPOCH_SENTINEL_UTC,
                last_check_passed=True,
                open_breaks=0,
                breaks_24h=0,
            ),
            EPOCH_SENTINEL_UTC,
        )
    risk_row = await repo.fetch_risk_state_current(account_id) or _default_risk_state(now)
    recon_row = await repo.fetch_reconciliation_summary(account_id)
    watchdog_ping = await repo.fetch_watchdog_last_ping_utc(account_id) or EPOCH_SENTINEL_UTC
    recon = ReconciliationSummary(
        last_check_utc=recon_row.last_check_utc or EPOCH_SENTINEL_UTC,
        last_check_passed=recon_row.last_check_passed,
        open_breaks=recon_row.open_breaks,
        breaks_24h=recon_row.breaks_24h,
    )
    return risk_row, recon, watchdog_ping


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/system/status",
    tags=["system"],
    response_model=SystemStatus,
)
async def system_status(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
    settings: APISettings = Depends(get_settings),
) -> SystemStatus:
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    risk_row, recon, watchdog_ping = await _load_status_components(repo, account_id, now)

    # ``halt_dwell_session_count`` is only meaningful in HALT_NEW state; in
    # other states the spec says null. Same shape decision for
    # convalescent_session_count.
    halt_dwell = None
    convalescent_count: int | None = None
    if risk_row.state == "HALT_NEW":
        halt_dwell = 0  # Phase 0 placeholder; dispatcher tracks the real counter
    elif risk_row.state == "CONVALESCENT":
        convalescent_count = risk_row.convalescent_session_count

    return SystemStatus(
        risk_state=risk_row.state,
        severity=risk_row.severity,
        halt_reason=risk_row.reason if risk_row.state == "HALT_NEW" else None,
        halt_dwell_session_count=halt_dwell,
        convalescent_session_count=convalescent_count,
        vacation_active=risk_row.vacation_active,
        vacation_until_utc=risk_row.vacation_until_utc,
        watchdog_last_ping_utc=watchdog_ping,
        reconciliation_summary=recon,
        is_session_active=False,  # Phase 0: CME-session boolean wires Week 5 Tue with SSE
        server_now=now,
        backend_version=settings.version,
        expected_frontend_version=settings.version,
    )


@router.get(
    "/api/system/kill-switch",
    tags=["system"],
    response_model=KillSwitchStatus,
)
async def kill_switch_status(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> KillSwitchStatus:
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    risk_row = (
        await repo.fetch_risk_state_current(account_id) if account_id else None
    ) or _default_risk_state(now)
    audit_uuid = (
        str(risk_row.audit_event_uuid) if risk_row.audit_event_uuid != UUID(int=0) else None
    )
    return KillSwitchStatus(
        risk_state=risk_row.state,
        severity=risk_row.severity,
        halt_reason=risk_row.reason if risk_row.state == "HALT_NEW" else None,
        last_transition_utc=risk_row.entered_at_utc if audit_uuid else None,
        last_transition_audit_event_uuid=audit_uuid,
    )


# ---------------------------------------------------------------------------
# Write endpoints — Day 25 wires the real dispatcher
# ---------------------------------------------------------------------------

# Per backend-spec §10.4 the api process is on "paper" environment in Phase 0
# (Hetzner Ashburn). ``phase_at_emit`` is 0 (Phase 0). Both values flow into
# the audit_log row via append_audit_event; the env is also CHECK-constrained
# on the audit_log column.
_PHASE_AT_EMIT_PHASE_0: Final[Literal[0]] = 0


def _triggered_by_for_session(
    session: SessionContext,
) -> Literal["risk_engine", "agent", "operator", "watchdog"]:
    """Map a SessionContext to the ``triggered_by`` enum value.

    The state_machine plan signature constrains ``triggered_by`` to
    ``{"risk_engine", "agent", "operator", "watchdog"}``. Today the only
    callers are the human operator (web UI) or the Discord bot acting on
    the operator's behalf — both map to ``"operator"``. The risk_engine /
    agent / watchdog values land when those callers wire in Phase 1+.
    """
    return "operator"


def _env_for_audit(
    environment: Literal["dev", "paper", "live-small", "live-scale"],
) -> Environment:
    """Map ``APISettings.environment`` to the audit_log ``env`` enum.

    Backend-spec §3.30 + alembic 0001 CHECK constraint allow only
    ``paper``, ``live-small``, ``live-scale``. ``dev`` is an api-process
    setting that has no analog in the audit log; in Phase-0 local dev we
    write audit rows tagged ``paper`` for consistency with how the VPS
    deploys (also paper). Production never sees ``dev``.
    """
    if environment == "dev":
        return "paper"
    return environment


@router.post(
    "/api/system/kill-switch/invoke",
    tags=["system"],
    response_model=KillSwitchTransitionResponse,
)
async def invoke_kill_switch(
    body: KillSwitchInvokeRequest,
    session: SessionContext = Depends(get_session_context),
    db: AsyncSession = Depends(get_session),
    repo: Phase1QueryRepo = Depends(_get_repo),
    settings: APISettings = Depends(get_settings),
) -> KillSwitchTransitionResponse:
    """Manual kill-switch invocation: NORMAL/CONVALESCENT → HALT_NEW.

    Per backend-spec §4.1.3 + §2.4.3. The route delegates to
    :mod:`services.risk.state_machine` for the plan and
    :mod:`services.risk.dispatch` for the I/O. The SSE ``risk_state``
    envelope is emitted AFTER audit + state writes succeed.

    Conflicts:

      * No active account → 409 ``NO_ACTIVE_ACCOUNT``.
      * ``risk_state == HALT_NEW`` already → 409 ``ALREADY_HALTED``
        (idempotent at facade level; the policy layer rejects
        HALT_NEW → HALT_NEW with :class:`IllegalTransitionError`).
    """
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        raise AppError(
            error_code="NO_ACTIVE_ACCOUNT",
            message=(
                "No active account is registered. Complete /api/setup/verify-token "
                "before invoking the kill switch."
            ),
            status_code=409,
        )

    risk_row = await repo.fetch_risk_state_current(account_id)
    if risk_row is not None and risk_row.state == "HALT_NEW":
        raise AppError(
            error_code="ALREADY_HALTED",
            message=(
                f"Kill switch already engaged (severity={risk_row.severity}, "
                f"reason={risk_row.reason!r}). Use /api/system/kill-switch/resume "
                "to recover."
            ),
            status_code=409,
        )

    current_state = RiskState(risk_row.state) if risk_row is not None else RiskState.NORMAL
    current_severity = (
        HaltSeverity(risk_row.severity)
        if risk_row is not None and risk_row.severity is not None
        else None
    )
    convalescent_counter = risk_row.convalescent_session_count if risk_row is not None else 0

    plan = plan_invoke_kill_switch(
        current_state=current_state,
        current_severity=current_severity,
        convalescent_counter=convalescent_counter,
        trigger=TransitionTrigger(body.trigger),
        triggered_by=_triggered_by_for_session(session),
        timestamp_utc=now.isoformat(),
    )

    # DP-021 (Day 25 carryover): the repo SELECTs above implicitly open a
    # transaction on this session (SQLAlchemy 2.0 autobegin semantics). The
    # audit writer's contract — services/audit/writer.py:58 — requires a
    # session with NO open transaction (it manages its own SERIALIZABLE +
    # advisory-lock block). Commit here to close the implicit read-side
    # transaction before apply_state_transition opens its writer transactions.
    # The reads were no-modify SELECTs so this commit is a no-op for data;
    # it only releases the transaction state.
    await db.commit()

    applied = await apply_state_transition(
        plan=plan,
        db=db,
        account_id=account_id,
        env=_env_for_audit(settings.environment),
        phase_at_emit=_PHASE_AT_EMIT_PHASE_0,
    )

    sequence_no = await emit_sse(
        plan.sse_event.event_type,
        {
            **plan.sse_event.data,
            "audit_event_uuid": str(applied.state_transition_audit_event_uuid),
        },
    )

    log.info(
        "kill_switch_invoke_applied",
        trigger=body.trigger,
        reason=body.reason,
        prior_state=current_state.value,
        new_state=applied.new_state,
        new_severity=applied.new_severity,
        audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        sse_sequence_no=sequence_no,
    )

    return KillSwitchTransitionResponse(
        risk_state=applied.new_state,
        severity=applied.new_severity,
        halt_reason=plan.reason,
        audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        sse_sequence_no=sequence_no,
    )


@router.post(
    "/api/system/kill-switch/resume",
    tags=["system"],
    response_model=KillSwitchTransitionResponse,
)
async def resume_from_halt(
    body: KillSwitchResumeRequest,
    session: SessionContext = Depends(get_session_context),
    db: AsyncSession = Depends(get_session),
    repo: Phase1QueryRepo = Depends(_get_repo),
    settings: APISettings = Depends(get_settings),
) -> KillSwitchTransitionResponse:
    """Resume from HALT_NEW → CONVALESCENT.

    Per backend-spec §4.1.3 + §2.4.3. Requires a recent WebAuthn UV
    (dev-guide §1.5 LOCKED 5-minute window). The state-machine policy
    enforces ``incident_review_id`` REQUIRED when current severity is
    ``incident_review`` — bare body for routine / defensive_envelope.

    Conflicts:

      * Re-auth missing → 401 ``RE_AUTH_REQUIRED``.
      * No active account → 409 ``NO_ACTIVE_ACCOUNT``.
      * ``risk_state != HALT_NEW`` → 409 ``NOT_HALTED``.
      * incident_review severity without ``incident_review_id`` → 422
        ``VALIDATION_ERROR`` (raised by the policy layer's
        :class:`IllegalTransitionError`, translated below).
    """
    sessions_mod.require_recent_uv(session)

    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        raise AppError(
            error_code="NO_ACTIVE_ACCOUNT",
            message="No active account is registered.",
            status_code=409,
        )

    risk_row = await repo.fetch_risk_state_current(account_id)
    if risk_row is None or risk_row.state != "HALT_NEW":
        current = risk_row.state if risk_row is not None else "NORMAL"
        raise AppError(
            error_code="NOT_HALTED",
            message=(
                f"Cannot resume from state {current!r}; resume is only valid "
                "from HALT_NEW. Use /api/system/kill-switch to inspect the "
                "current state."
            ),
            status_code=409,
        )

    current_severity = HaltSeverity(risk_row.severity) if risk_row.severity is not None else None

    # The policy layer's IllegalTransitionError on missing incident_review_id
    # is a 422-equivalent for the caller. Translate it to AppError(422) here
    # rather than letting the policy exception propagate as a 500.
    try:
        plan = plan_resume_from_halt(
            current_state=RiskState.HALT_NEW,
            current_severity=current_severity,
            operator_session_id=session.user_id,
            incident_review_id=body.incident_review_id,
            timestamp_utc=now.isoformat(),
        )
    except IllegalTransitionError as exc:
        # incident_review_id is required for severity=incident_review per
        # backend-spec §2.4.3. Other IllegalTransitionError branches are
        # already filtered out by the NOT_HALTED 409 check above, so a
        # raised IllegalTransitionError here can only be the
        # incident_review_id-missing case.
        raise AppError(
            error_code="INCIDENT_REVIEW_ID_REQUIRED",
            message=str(exc),
            status_code=422,
        ) from exc

    # DP-021 (Day 25 carryover): see invoke route comment. The repo SELECTs
    # above auto-began a transaction on this session; the writer requires a
    # clean session, so commit here before apply_state_transition.
    await db.commit()

    applied = await apply_state_transition(
        plan=plan,
        db=db,
        account_id=account_id,
        env=_env_for_audit(settings.environment),
        phase_at_emit=_PHASE_AT_EMIT_PHASE_0,
    )

    sequence_no = await emit_sse(
        plan.sse_event.event_type,
        {
            **plan.sse_event.data,
            "audit_event_uuid": str(applied.state_transition_audit_event_uuid),
        },
    )

    log.info(
        "kill_switch_resume_applied",
        operator_session_id=session.user_id,
        prior_severity=current_severity.value if current_severity else None,
        incident_review_id=body.incident_review_id,
        audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        sse_sequence_no=sequence_no,
    )

    return KillSwitchTransitionResponse(
        risk_state=applied.new_state,
        severity=applied.new_severity,
        halt_reason=None,  # CONVALESCENT carries no halt_reason
        audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        sse_sequence_no=sequence_no,
    )


__all__ = ["router"]
