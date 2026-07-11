"""services/api/routes/system.py — `/api/system/*` Phase-1 endpoints.

Day 15 (Week 5 Mon) shipped:

  * ``GET  /api/system/status`` — composite snapshot for the Today tile.
  * ``GET  /api/system/kill-switch`` — narrow projection of the kill-switch
    state (a strict subset of /system/status; called by the dedicated
    kill-switch UI tile).
  * ``POST /api/system/kill-switch/invoke`` — body-validated 501 stub.
  * ``POST /api/system/kill-switch/resume`` — body-validated 501 stub.

Day 25 (Week 7 Mon) unstubbed the two POST endpoints end-to-end:

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

Day 27 (Week 7 Wed) adds three read-only surfaces consumed by the
`/system` page (frontend-spec §2.6.3/§2.6.4/§2.6.6):

  * ``GET /api/system/risk-envelope`` — Phase 1 read-only view of the
    active parameter set. Phase 0 returns LOCKED spec defaults (§2.4)
    because the ``parameter_sets`` table is empty until the Week 4 Wed
    dispatcher writes the first row.
  * ``GET /api/system/audit`` — audit explorer with filter + cursor.
    Reads the partition-pruned ``audit_log`` heap and returns canonical
    payload + chain metadata.
  * ``GET /api/system/watchdog`` — RETIRED 2026-07-09 (route + schema
    removed): the external Nuremberg watchdog box was cancelled in favor
    of a hosted uptime monitor (see Docs/decisions-log.md 2026-07-09).
    ``SystemStatus.watchdog_last_ping_utc`` is retained as a permanent
    epoch sentinel for wire-compat.

Conflict handling on the POST endpoints (Day 25):

  * ``invoke`` while ``risk_state=HALT_NEW`` returns 409
    ``ALREADY_HALTED``.
  * ``resume`` while ``risk_state != HALT_NEW`` returns 409 ``NOT_HALTED``.

NOT in scope today (deferred to later sessions):

  * ``POST /api/system/vacation/{start,end}``
  * ``GET /api/system/audit/export.csv``
  * ``GET /api/system/deployments`` / rollback (Phase 2)
  * ``GET /api/system/agent-activity`` (Phase 2)
  * ``GET /api/system/costs`` (Phase 2)

The ``server_now`` field is computed at response build time per spec
§4.1.6. The ``backend_version`` echoes ``APISettings.version``;
``expected_frontend_version`` is hardcoded to the same value in Phase 0
(no version-skew enforcement until Week 6 frontend ships).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.auth import sessions as sessions_mod
from services.api.config import APISettings, get_settings
from services.api.db import get_session
from services.api.errors import AppError
from services.api.heartbeats import (
    HEARTBEAT_STALE_THRESHOLDS_S,
    HeartbeatRegistry,
    HeartbeatService,
    classify_freshness,
    get_heartbeat_registry,
)
from services.api.repos.cycle import CycleQueryRepo, PostgresCycleQueryRepo
from services.api.repos.funding import FundingQueryRepo, PostgresFundingQueryRepo
from services.api.repos.phase1 import (
    AuditLogRow,
    ParameterSetRow,
    Phase1QueryRepo,
    PostgresPhase1QueryRepo,
    RiskStateRow,
)
from services.api.schemas.capital_events import (
    CapitalEventInvokeRequest,
    CapitalEventInvokeResponse,
)
from services.api.schemas.common import EPOCH_SENTINEL_UTC
from services.api.schemas.cycle import (
    SystemCycleResponse,
    decision_row_to_view,
    worker_row_to_view,
)
from services.api.schemas.funding import (
    SystemFundingResponse,
    build_system_funding_response,
)
from services.api.schemas.system import (
    AuditLogEntry,
    AuditLogPageResponse,
    HeartbeatItem,
    HeartbeatsResponse,
    KillSwitchFalsePositiveRequest,
    KillSwitchInvokeRequest,
    KillSwitchResumeRequest,
    KillSwitchStatus,
    KillSwitchTransitionResponse,
    ReconciliationSummary,
    RiskEnvelopeResponse,
    RiskParameterItem,
    SystemStatus,
)
from services.api.session import SessionContext, get_session_context
from services.api.sse import emit_sse
from services.audit.writer import Environment
from services.risk.capital_events import (
    CapitalEventError,
    apply_capital_event,
    plan_capital_event,
)
from services.risk.dispatch import apply_state_transition
from services.risk.state_machine import (
    HaltSeverity,
    IllegalTransitionError,
    RiskState,
    TransitionTrigger,
    plan_false_positive_graduation,
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

    External watchdog retired 2026-07-09 (hosted uptime monitor replaced
    it); with no liveness writer, the watchdog ping component is
    permanently the epoch sentinel until/unless a future writer exists.
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
    "/api/system/capital-event",
    tags=["system"],
    response_model=CapitalEventInvokeResponse,
)
async def invoke_capital_event(
    body: CapitalEventInvokeRequest,
    session: SessionContext = Depends(get_session_context),
    db: AsyncSession = Depends(get_session),
    repo: Phase1QueryRepo = Depends(_get_repo),
    settings: APISettings = Depends(get_settings),
) -> CapitalEventInvokeResponse:
    """Record a deposit or withdrawal capital event.

    Per cutover plan §7 + backend-spec §3.20 + §2.4.4. Reads the latest
    ``balances.net_liquidation`` as ``pre_event_equity`` (or 0 on a fresh
    live DB — the bootstrap-deposit case). Delegates to
    :mod:`services.risk.capital_events` for the plan + apply.

    Errors:

    * ``NO_ACTIVE_ACCOUNT`` (409) — no accounts row; run
      ``scripts/operator_tools/bootstrap_live_account.py`` first.
    * ``AMOUNT_INVALID`` (422) — amount_usd is not a valid Decimal or
      is non-positive.
    * ``PRE_EVENT_EQUITY_NEGATIVE`` (422) — defensive; should be
      impossible since the route clamps to 0.
    * ``WITHDRAWAL_EXCEEDS_EQUITY`` (422) — withdrawal > current
      balance; operator clamps at the available balance.
    """
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        raise AppError(
            error_code="NO_ACTIVE_ACCOUNT",
            message=(
                "No active account is registered. Run "
                "scripts/operator_tools/bootstrap_live_account.py "
                "before invoking a capital event."
            ),
            status_code=409,
        )

    try:
        amount_usd = Decimal(body.amount_usd)
    except (ArithmeticError, ValueError) as exc:
        raise AppError(
            error_code="AMOUNT_INVALID",
            message=f"amount_usd is not a valid Decimal: {exc}",
            status_code=422,
        ) from exc

    # pre_event_equity from the latest balances snapshot. Bootstrap case
    # (no balances row yet — fresh live DB): treat as 0; the planner
    # short-circuits to threshold_met=True per cutover plan §7 step 4.
    pre_event_equity = await repo.fetch_latest_balance_nav(account_id)
    if pre_event_equity is None:
        pre_event_equity = Decimal("0")

    try:
        plan = plan_capital_event(
            account_id=account_id,
            event_type=body.event_type,
            amount_usd=amount_usd,
            pre_event_equity=pre_event_equity,
            current_session_no=body.current_session_no,
            operator_reason=body.reason,
            now_utc=now,
        )
    except CapitalEventError as exc:
        raise AppError(
            error_code=exc.error_code,
            message=str(exc),
            status_code=422,
        ) from exc

    # DP-021 (Day 25 carryover) — same pattern as invoke_kill_switch: close
    # the implicit read-side transaction before the audit writer opens its
    # own SERIALIZABLE block.
    await db.commit()

    applied = await apply_capital_event(
        plan=plan,
        db=db,
        env=_env_for_audit(settings.environment),
        phase_at_emit=_PHASE_AT_EMIT_PHASE_0,
    )

    log.info(
        "capital_event_invoked",
        event_type=body.event_type,
        amount_usd=body.amount_usd,
        operator_reason=body.reason,
        capital_event_id=str(applied.capital_event_id),
        threshold_met=applied.threshold_met,
        capital_event_audit_event_uuid=str(applied.capital_event_audit_event_uuid),
        mode_started_audit_event_uuid=(
            str(applied.mode_started_audit_event_uuid)
            if applied.mode_started_audit_event_uuid
            else None
        ),
    )

    return CapitalEventInvokeResponse(
        capital_event_id=str(applied.capital_event_id),
        event_type=applied.event_type,
        amount_usd=str(applied.amount_usd),
        threshold_met=applied.threshold_met,
        post_event_equity=str(applied.post_event_equity),
        capital_event_audit_event_uuid=str(applied.capital_event_audit_event_uuid),
        mode_started_audit_event_uuid=(
            str(applied.mode_started_audit_event_uuid)
            if applied.mode_started_audit_event_uuid
            else None
        ),
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


@router.post(
    "/api/system/kill-switch/false-positive",
    tags=["system"],
    response_model=KillSwitchTransitionResponse,
)
async def adjudicate_false_positive(
    body: KillSwitchFalsePositiveRequest,
    session: SessionContext = Depends(get_session_context),
    db: AsyncSession = Depends(get_session),
    repo: Phase1QueryRepo = Depends(_get_repo),
    settings: APISettings = Depends(get_settings),
) -> KillSwitchTransitionResponse:
    """Adjudicate the current CONVALESCENT probation as a false positive
    and graduate to NORMAL immediately (2026-07-11 operator amendment).

    Same auth bar as ``resume`` (risk-LOOSENING → recent WebAuthn UV per
    dev-guide §1.5 LOCKED; Discord stays risk-tightening-only per
    frontend-spec §6.1). Only lawful from CONVALESCENT — a false-positive
    claim never shortcuts a live HALT_NEW; resume first, then adjudicate.

    Conflicts:

      * Re-auth missing → 401 ``RE_AUTH_REQUIRED``.
      * No active account → 409 ``NO_ACTIVE_ACCOUNT``.
      * ``risk_state != CONVALESCENT`` → 409 ``NOT_CONVALESCENT``.
      * Blank reason / defect_reference → 422 (pydantic min_length).
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
    if risk_row is None or risk_row.state != "CONVALESCENT":
        current = risk_row.state if risk_row is not None else "NORMAL"
        raise AppError(
            error_code="NOT_CONVALESCENT",
            message=(
                f"Cannot adjudicate a false positive from state {current!r}; "
                "adjudication is only valid from CONVALESCENT. Resume the "
                "halt first if the system is in HALT_NEW."
            ),
            status_code=409,
        )

    # Whitespace-only reason/reference passes pydantic's min_length but is
    # rejected by the policy layer — translate to 422 rather than 500.
    try:
        plan = plan_false_positive_graduation(
            current_state=RiskState.CONVALESCENT,
            convalescent_counter=risk_row.convalescent_session_count,
            operator_reason=body.reason,
            defect_reference=body.defect_reference,
            operator_session_id=session.user_id,
            timestamp_utc=now.isoformat(),
        )
    except IllegalTransitionError as exc:
        raise AppError(
            error_code="VALIDATION_ERROR",
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
        "kill_switch_false_positive_applied",
        operator_session_id=session.user_id,
        defect_reference=body.defect_reference,
        convalescent_sessions_served=risk_row.convalescent_session_count,
        audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        sse_sequence_no=sequence_no,
    )

    return KillSwitchTransitionResponse(
        risk_state=applied.new_state,
        severity=applied.new_severity,
        halt_reason=None,
        audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        sse_sequence_no=sequence_no,
    )


# ---------------------------------------------------------------------------
# Day 27 — Risk envelope (read-only)
# ---------------------------------------------------------------------------

# LOCKED Phase 0 defaults per backend-spec §2.4 + frontend-spec §2.6.3.
# These mirror the displayed values verbatim; when the Week 4 Wed dispatcher
# writes the first ``parameter_sets`` row the route swaps to reading from the
# table and ``source`` flips to ``"parameters_table"``.
_DEFAULT_RISK_ENVELOPE: Final[tuple[RiskParameterItem, ...]] = (
    RiskParameterItem(
        key="vol_target_annual",
        label="Vol target",
        value=Decimal("0.14"),
        unit="percent",
    ),
    RiskParameterItem(
        key="per_position_target",
        label="Per-position target",
        value=Decimal("0.25"),
        unit="percent",
    ),
    RiskParameterItem(
        key="per_position_hard_floor",
        label="Per-position hard floor",
        value=Decimal("0.50"),
        unit="percent",
    ),
    RiskParameterItem(
        key="gross_exposure_cap",
        label="Gross exposure cap",
        value=Decimal("3.00"),
        unit="percent",
    ),
    RiskParameterItem(
        key="net_exposure_cap",
        label="Net exposure cap",
        value=Decimal("1.50"),
        unit="percent",
    ),
    RiskParameterItem(
        key="cluster_cap_equity_index",
        label="Cluster cap: equity-idx",
        value=Decimal("0.60"),
        unit="percent",
        cluster="equity_index",
    ),
    RiskParameterItem(
        key="cluster_cap_rates_bonds",
        label="Cluster cap: rates/bonds",
        value=Decimal("0.80"),
        unit="percent",
        cluster="rates_bonds",
    ),
    RiskParameterItem(
        key="cluster_cap_commodity",
        label="Cluster cap: commodity",
        value=Decimal("0.80"),
        unit="percent",
        cluster="commodity",
    ),
    RiskParameterItem(
        key="cluster_cap_crypto",
        label="Cluster cap: crypto",
        value=Decimal("0.40"),
        unit="percent",
        cluster="crypto",
    ),
    RiskParameterItem(
        key="cluster_cap_fx",
        label="Cluster cap: FX",
        value=Decimal("0.30"),
        unit="percent",
        cluster="fx",
    ),
    RiskParameterItem(
        key="correlation_alert_threshold",
        label="Realized correlation alert",
        value=Decimal("0.70"),
        unit="ratio",
    ),
    RiskParameterItem(
        key="correlation_halt_threshold",
        label="Realized correlation halt",
        value=Decimal("0.85"),
        unit="ratio",
    ),
    RiskParameterItem(
        key="daily_loss_limit",
        label="Daily loss limit",
        value=Decimal("-0.05"),
        unit="percent",
    ),
    RiskParameterItem(
        key="trailing_drawdown_limit",
        label="Trailing DD limit",
        value=Decimal("-0.20"),
        unit="percent",
    ),
    RiskParameterItem(
        key="monthly_drawdown_vol_halve",
        label="Monthly DD vol-halve",
        value=Decimal("-0.10"),
        unit="percent",
    ),
)


def _parameter_set_to_items(
    row: ParameterSetRow,
) -> list[RiskParameterItem] | None:
    """Walk a populated ``parameter_sets`` JSONB into RiskParameterItem rows.

    Returns None when the row's JSONB doesn't carry the canonical keys —
    the route then falls back to spec defaults rather than rendering a
    partially-filled envelope. The canonical key set is the 15 keys from
    ``_DEFAULT_RISK_ENVELOPE``; Phase 1+ may extend the parameter set
    schema but must keep these keys present.

    Phase 0 reality: this path is dead code today because
    ``fetch_active_parameter_set`` returns None against an empty table.
    Lock it now so the wire shape doesn't shift when the dispatcher
    finally writes a row.
    """
    items: list[RiskParameterItem] = []
    for default in _DEFAULT_RISK_ENVELOPE:
        raw = row.parameters.get(default.key)
        if raw is None:
            return None
        try:
            value = Decimal(str(raw))
        except (ValueError, TypeError):
            return None
        items.append(
            RiskParameterItem(
                key=default.key,
                label=default.label,
                value=value,
                unit=default.unit,
                cluster=default.cluster,
            )
        )
    return items


@router.get(
    "/api/system/risk-envelope",
    tags=["system"],
    response_model=RiskEnvelopeResponse,
)
async def risk_envelope(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> RiskEnvelopeResponse:
    """Phase 1 read-only view of the active risk envelope.

    Phase 0: ``parameter_sets`` table is empty, the route returns
    ``source="spec_defaults"`` with the §2.4 LOCKED values inlined above.
    When the dispatcher writes the first row this route auto-flips to
    ``source="parameters_table"`` and reads via
    :meth:`Phase1QueryRepo.fetch_active_parameter_set` without further
    code changes here.
    """
    now = datetime.now(tz=UTC)
    row = await repo.fetch_active_parameter_set()
    if row is not None:
        items_from_db = _parameter_set_to_items(row)
        if items_from_db is not None:
            return RiskEnvelopeResponse(
                items=items_from_db,
                parameter_set_hash=row.parameter_set_hash,
                parameter_set_active_since_utc=row.first_active_at,
                source="parameters_table",
                server_now=now,
            )
        log.warning(
            "risk_envelope_parameter_set_missing_keys",
            parameter_set_hash=row.parameter_set_hash,
            present_keys=sorted(row.parameters.keys()),
        )
    return RiskEnvelopeResponse(
        items=list(_DEFAULT_RISK_ENVELOPE),
        parameter_set_hash=None,
        parameter_set_active_since_utc=None,
        source="spec_defaults",
        server_now=now,
    )


# ---------------------------------------------------------------------------
# Day 27 — Audit explorer (read-only)
# ---------------------------------------------------------------------------

# Payload preview is the first 80 chars of the decoded JCS string per
# frontend-spec §2.6.4 "preview (first 80 chars of payload reason)".
_PAYLOAD_PREVIEW_CHARS: Final[int] = 80


def _audit_row_to_entry(row: AuditLogRow) -> AuditLogEntry:
    """Map a raw audit_log row to the §2.6.4 wire entry.

    Decodes ``payload_jcs`` as UTF-8 with ``errors='replace'`` so a
    malformed payload (shouldn't happen — JCS is ASCII-only by RFC 8785)
    doesn't crash the response. Hashes hex-encoded so operators paste
    into runbooks verbatim.
    """
    preview = row.payload_jcs.decode("utf-8", errors="replace")[:_PAYLOAD_PREVIEW_CHARS]
    return AuditLogEntry(
        event_uuid=str(row.event_uuid),
        sequence_no=row.sequence_no,
        event_type=row.event_type,
        env=row.env,  # type: ignore[arg-type]  # Pydantic Literal coercion from str
        phase_at_emit=row.phase_at_emit,
        source_clock_ts=row.source_clock_ts,
        ingest_clock_ts=row.ingest_clock_ts,
        prev_hash_hex=row.prev_hash.hex(),
        record_hash_hex=row.record_hash.hex(),
        repaired_for_sequence_no=row.repaired_for_sequence_no,
        repaired_for_event_timestamp=row.repaired_for_event_timestamp,
        payload_preview=preview,
    )


def _decode_audit_cursor(cursor: str | None) -> int | None:
    """Decode an audit-log cursor into the ``before_sequence_no`` int.

    Cursor format: the decimal string of the last-seen ``sequence_no``.
    Operator-readable + lock-free + globally unique (BIGSERIAL).
    Falls back to a canonical ``INVALID_CURSOR`` AppError per the
    ``_pagination.py`` contract on any parse failure.
    """
    if cursor is None:
        return None
    try:
        seq = int(cursor)
    except (ValueError, TypeError) as exc:
        raise AppError(
            error_code="INVALID_CURSOR",
            message=("Audit-log cursor is malformed; restart pagination from the top."),
            status_code=400,
            details={"reason": type(exc).__name__},
        ) from exc
    if seq <= 0:
        raise AppError(
            error_code="INVALID_CURSOR",
            message=(
                "Audit-log cursor must be a positive sequence_no; restart pagination from the top."
            ),
            status_code=400,
        )
    return seq


@router.get(
    "/api/system/audit",
    tags=["system"],
    response_model=AuditLogPageResponse,
)
async def audit_log_page(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
    event_type: str | None = Query(default=None, max_length=128),
    env: Literal["paper", "live-small", "live-scale"] | None = Query(default=None),
    from_utc: datetime | None = Query(default=None, alias="from"),
    to_utc: datetime | None = Query(default=None, alias="to"),
    cursor: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
) -> AuditLogPageResponse:
    """Audit explorer page per backend-spec §4.1.3 + frontend-spec §2.6.4.

    Query params (all optional):

      * ``event_type`` — exact match against the locked taxonomy.
      * ``env`` — partition filter (paper / live-small / live-scale).
      * ``from`` / ``to`` — ``ingest_clock_ts`` window for partition
        pruning.
      * ``cursor`` — decimal sequence_no; returns rows STRICTLY less than
        the cursor in DESC order.
      * ``limit`` — 1..200 (defaults to 50).
    """
    before_seq = _decode_audit_cursor(cursor)
    rows, next_seq, has_more = await repo.fetch_audit_log_page(
        event_type=event_type,
        env=env,
        from_ingest_utc=from_utc,
        to_ingest_utc=to_utc,
        before_sequence_no=before_seq,
        limit=limit,
    )
    return AuditLogPageResponse(
        entries=[_audit_row_to_entry(r) for r in rows],
        next_cursor=str(next_seq) if next_seq is not None else None,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# Service-heartbeat freshness (this PR — silent-failure closure)
# ---------------------------------------------------------------------------


_HEARTBEAT_SERVICES: Final[tuple[HeartbeatService, ...]] = ("lean_cycle",)


def _get_heartbeat_registry() -> HeartbeatRegistry:
    """FastAPI dependency hook so tests can override with a fresh registry."""
    return get_heartbeat_registry()


@router.get(
    "/api/system/heartbeats",
    tags=["system"],
    response_model=HeartbeatsResponse,
)
async def list_heartbeats(
    session: SessionContext = Depends(get_session_context),
    registry: HeartbeatRegistry = Depends(_get_heartbeat_registry),
) -> HeartbeatsResponse:
    """Per-service heartbeat freshness for the System page.

    Closes a silent-failure surface: pre-this-endpoint, LEAN could stop
    cycling and the operator would only notice when the next /signals
    refresh showed yesterday's data. This endpoint exposes the in-memory
    :class:`services.api.heartbeats.HeartbeatRegistry` (updated by the
    LEAN POST path on every ``lean_cycle_heartbeat`` /
    ``lean_strategy_initialized`` event).

    Phase 1 ships ``lean_cycle`` only. Follow-up PRs extend
    ``_HEARTBEAT_SERVICES`` with ``reconciliation_eod`` +
    ``order_placement_worker`` as those producers also call
    :meth:`HeartbeatRegistry.record`.

    Empty registry (api just booted; no heartbeats since restart) →
    every item carries ``freshness='unknown'`` + null timestamps. NOT
    rendered as a fault — that's the documented "first-cycle pending"
    branch in the frontend tile.
    """
    now = datetime.now(tz=UTC)
    snapshot = registry.snapshot()
    items: list[HeartbeatItem] = []
    for svc in _HEARTBEAT_SERVICES:
        record = snapshot.get(svc)
        freshness = classify_freshness(svc, record, now_utc=now)
        if record is None:
            seconds_since: int | None = None
            last_hb: datetime | None = None
            received_at: datetime | None = None
            count = 0
        else:
            seconds_since = max(int((now - record.last_heartbeat_utc).total_seconds()), 0)
            last_hb = record.last_heartbeat_utc
            received_at = record.received_at_utc
            count = record.count
        items.append(
            HeartbeatItem(
                service=svc,
                last_heartbeat_utc=last_hb,
                received_at_utc=received_at,
                seconds_since_last_heartbeat=seconds_since,
                stale_threshold_s=HEARTBEAT_STALE_THRESHOLDS_S[svc],
                freshness=freshness,
                count=count,
            )
        )
    return HeartbeatsResponse(items=items, server_now=now)


# ---------------------------------------------------------------------------
# Crypto-pivot §3.7 — daily-cycle status (replaces bar-sync status)
# ---------------------------------------------------------------------------

#: CDE halts trading Fridays 17:00-18:00 America/New_York (strategy §1.6 /
#: §7 weekly-close handling). The close is an ET wall-clock event, so the
#: UTC instant shifts with DST (21:00Z summer / 22:00Z winter).
_CDE_ET = ZoneInfo("America/New_York")
_CDE_CLOSE_WEEKDAY: Final[int] = 4  # Friday
_CDE_CLOSE_HOUR_ET: Final[int] = 17


def next_friday_cde_close_utc(now: datetime) -> datetime:
    """Next Friday 17:00 ET (CDE weekly close start), as a UTC instant.

    Exactly at the close boundary rolls to next week's close (the current
    one is no longer "next"). Naive input rejected per [A06].
    """
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware UTC; got naive datetime")
    et_now = now.astimezone(_CDE_ET)
    days_ahead = (_CDE_CLOSE_WEEKDAY - et_now.weekday()) % 7
    candidate = (et_now + timedelta(days=days_ahead)).replace(
        hour=_CDE_CLOSE_HOUR_ET, minute=0, second=0, microsecond=0
    )
    if candidate <= et_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def _get_cycle_repo(session: AsyncSession = Depends(get_session)) -> CycleQueryRepo:
    return PostgresCycleQueryRepo(session)


@router.get(
    "/api/system/cycle",
    tags=["system"],
    response_model=SystemCycleResponse,
)
async def system_cycle(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
    cycle_repo: CycleQueryRepo = Depends(_get_cycle_repo),
) -> SystemCycleResponse:
    """Daily-cycle projection per delta spec §3.7 (read-only).

    Composes three components:

      * **Last decision outcome** — latest ``strategy_decisions`` row for
        the active account: per-asset trend score → target → action →
        est. costs from the worker's ``strategy_decision_v1`` outcome
        JSONB. Skip/failed rows surface their ``skip_reason``.
      * **Last risk-loop heartbeat** — ``strategy_worker_status`` row +
        freshness classification (30 s loop; fresh ≤ 90 s).
      * **Next Friday CDE close** — the weekly 17:00 ET halt, UTC-anchored.

    Fresh DB / worker-never-ran contract: ``decision`` and ``worker`` are
    null (HTTP 200, not an error) — consumers (the §3.8 ``/cycle`` Discord
    digest, the §3.9 pipeline-freshness strip) render the no-decision-yet
    state.
    """
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    decision_view = None
    worker_view = None
    if account_id is not None:
        decision_row = await cycle_repo.fetch_latest_decision(account_id)
        if decision_row is not None:
            decision_view = decision_row_to_view(decision_row)
        status_row = await cycle_repo.fetch_worker_status(account_id)
        if status_row is not None:
            worker_view = worker_row_to_view(status_row, now=now)
    return SystemCycleResponse(
        decision=decision_view,
        worker=worker_view,
        next_friday_close_utc=next_friday_cde_close_utc(now),
        server_now=now,
    )


# ---------------------------------------------------------------------------
# Crypto-pivot §3.7/§3.9 — funding + cash-yield telemetry
# ---------------------------------------------------------------------------


def _get_funding_repo(session: AsyncSession = Depends(get_session)) -> FundingQueryRepo:
    return PostgresFundingQueryRepo(session)


@router.get(
    "/api/system/funding",
    tags=["system"],
    response_model=SystemFundingResponse,
)
async def system_funding(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
    funding_repo: FundingQueryRepo = Depends(_get_funding_repo),
    settings: APISettings = Depends(get_settings),
) -> SystemFundingResponse:
    """Funding + cash-yield telemetry per delta spec §3.7/§3.9 (read-only).

    Composes four components for the Today-page funding strip:

      * **Per-product funding** — latest hourly ``funding_rates`` snapshot
        per product (§3.2 logger) + annualized rate, joined against held
        positions so the operator's book sorts first.
      * **Estimated funding today** — summed from today's hourly rows per
        held product. This is an ESTIMATE from telemetry, never venue-
        settled truth — ``funding_basis`` labels it honestly on the wire
        (delta spec §3.9's "accrued funding today" is an estimate strip).
      * **Cash sweeps** — today's §3.6 ``cash_sweeps`` ledger rows +
        completed to-yield/to-margin totals; empty until the C2
        cash_manager activates. ``yield_apy`` is the interim manual
        ``APISettings.cash_yield_apy`` (None renders as "—").
      * **Liquidation buffer** — from the latest
        ``balance_snapshot_recorded`` audit payload (00:15 UTC recon);
        the venue's ``1000`` no-risk sentinel maps to None.
      * **Cash balances + USDC rewards** — the daily 00:20 UTC capture
        (``services/data/usdc_rewards.py``): latest spot USD / CBI USDC
        balances, last positive USDC ledger credit (rewards pay out
        Fridays), lifetime reward credits total.

    Fresh DB contract: empty lists + nulls, HTTP 200 — consumers render
    the no-telemetry-yet state, never an error.
    """
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    held_positions = (
        await funding_repo.fetch_held_positions_with_contract(account_id)
        if account_id is not None
        else []
    )
    latest_rates = await funding_repo.fetch_latest_funding_rates()
    product_ids = sorted({r.product_id for r in latest_rates} | {p.market for p in held_positions})
    today_rates = await funding_repo.fetch_funding_rates_today(product_ids)
    metadata = await funding_repo.fetch_latest_product_metadata()
    sweeps_today = await funding_repo.fetch_cash_sweeps_today()
    balance_snapshot = await funding_repo.fetch_latest_balance_snapshot_audit()
    cash_snapshot = await funding_repo.fetch_latest_cash_balance_snapshot()
    last_reward = await funding_repo.fetch_last_reward_credit()
    lifetime_rewards = await funding_repo.fetch_lifetime_reward_credits_total()
    return build_system_funding_response(
        latest_rates=latest_rates,
        today_rates=today_rates,
        held_positions=held_positions,
        metadata=metadata,
        sweeps_today=sweeps_today,
        balance_snapshot=balance_snapshot,
        cash_snapshot=cash_snapshot,
        last_reward=last_reward,
        lifetime_rewards=lifetime_rewards,
        yield_apy=settings.cash_yield_apy,
        now=now,
    )


__all__ = ["router"]
