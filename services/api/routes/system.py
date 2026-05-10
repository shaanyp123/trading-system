"""services/api/routes/system.py — `/api/system/*` Phase-1 endpoints (subset).

Day 15 scope (per IG §3 Week 5 Mon):

  * ``GET  /api/system/status`` — composite snapshot for the Today tile.
  * ``GET  /api/system/kill-switch`` — narrow projection of the kill-switch
    state (a strict subset of /system/status; called by the dedicated
    kill-switch UI tile).
  * ``POST /api/system/kill-switch/invoke`` — request-body validation only;
    501 stub until Week 4 Wed dispatcher wires audit_log INSERT + risk_state
    UPDATE + SSE emit.
  * ``POST /api/system/kill-switch/resume`` — same 501 stub; same dispatcher.

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
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

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
    ReconciliationSummary,
    SystemStatus,
)
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


def _default_risk_state(now: datetime) -> RiskStateRow:
    """Return a synthetic NORMAL risk-state row when the table has no current row.

    Phase 0 reality: the ``risk_state`` table has no row until Week 4 Wed
    dispatcher's first INSERT. Rather than 500 on a missing row, we synthesize
    a NORMAL state with neutral counters. The synthetic row's ``audit_event_uuid``
    is the all-zero UUID — a sentinel readers can recognise as "no real
    transition has been recorded".
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
# Write endpoints — 501-stubbed until Week 4 Wed dispatcher PR
# ---------------------------------------------------------------------------


_KILL_SWITCH_NOT_WIRED_MESSAGE = (
    "Kill-switch invoke/resume handlers wire up in the Week 4 Wed dispatcher "
    "PR (services/risk/state_machine.plan_invoke_kill_switch + audit_log "
    "INSERT + SSE emit). Day 15 ships the route scaffold + body validation "
    "only."
)


@router.post(
    "/api/system/kill-switch/invoke",
    tags=["system"],
    status_code=501,
)
async def invoke_kill_switch(
    body: KillSwitchInvokeRequest,
    session: SessionContext = Depends(get_session_context),
) -> None:
    log.info(
        "kill_switch_invoke_stubbed",
        trigger=body.trigger,
        reason=body.reason,
    )
    raise AppError(
        error_code="KILL_SWITCH_HANDLER_NOT_WIRED",
        message=_KILL_SWITCH_NOT_WIRED_MESSAGE,
        status_code=501,
    )


@router.post(
    "/api/system/kill-switch/resume",
    tags=["system"],
    status_code=501,
)
async def resume_from_halt(
    body: KillSwitchResumeRequest,
    session: SessionContext = Depends(get_session_context),
) -> None:
    log.info(
        "kill_switch_resume_stubbed",
        incident_review_id=body.incident_review_id,
    )
    raise AppError(
        error_code="KILL_SWITCH_HANDLER_NOT_WIRED",
        message=_KILL_SWITCH_NOT_WIRED_MESSAGE,
        status_code=501,
    )


__all__ = ["router"]
