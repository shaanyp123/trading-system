"""services/api/routes/signals.py — `/api/signals*` Phase-1 endpoints.

Backend-spec §4.1.2:

  * ``GET  /api/signals?status=&limit=&cursor=`` — list signals.
  * ``POST /api/signals/:id/approve`` — approve a pending signal.
  * ``POST /api/signals/:id/reject``  — reject with embedded diary entry.
  * ``POST /api/signals/:id/defer``   — defer with embedded diary entry.

**Pivot-PR-D (post-pivot 2026-05-12) — unstubs the Day-15 501 handlers.**
The three POST endpoints now call into
``services.risk.signal_dispatch`` to write the audit event + UPDATE the
signals row + return the new status. Order placement (the actual
``IbkrClient.place_order()`` call for the approve path) is NOT in
PR-D scope; a follow-up consumes approved signals + places orders.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.errors import AppError
from services.api.repos.phase1 import (
    Phase1QueryRepo,
    PostgresPhase1QueryRepo,
)
from services.api.routes._pagination import clamp_limit
from services.api.schemas.signals import (
    SignalApproveRequest,
    SignalDeferRequest,
    SignalListResponse,
    SignalRejectRequest,
)
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


@router.get(
    "/api/signals",
    tags=["signals"],
    response_model=SignalListResponse,
)
async def list_signals(
    status: Literal[
        "pending",
        "approved",
        "rejected",
        "deferred",
        "expired",
        "working",
        "partially_filled",
        "filled",
        "cancelled",
        "closed",
        "stopped_out",
    ]
    | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> SignalListResponse:
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        # No account row exists yet (Week 6 setup_token flow creates it).
        # Return empty list rather than 404; the frontend's first paint
        # works against an empty signals list and re-fetches when the
        # account materializes.
        return SignalListResponse(items=[], next_cursor=None, has_more=False)

    _rows, next_cursor, has_more = await repo.fetch_signals_page(
        account_id,
        status=status,
        cursor=cursor,
        limit=clamp_limit(limit),
    )
    # Phase 0: signals table is empty, so _rows is always []. The deliberate
    # empty-list return is the spec's scaffold-only shape; the row-to-
    # SignalSummary mapper lands when the dispatcher PR (Week 4 Wed) wires
    # signal emission and we have real data to map.
    return SignalListResponse(
        items=[],
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def _resolve_account_id(repo: Phase1QueryRepo) -> UUID:
    """Resolve the active account_id; raise 409 if none configured.

    Phase 1 is single-account; this picks the operator's account. Future
    multi-account (Phase 3+) replaces this with the session's account
    scope.
    """
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        raise AppError(
            error_code="NO_ACTIVE_ACCOUNT",
            message="No active account configured; complete /setup first.",
            status_code=409,
        )
    return account_id


async def _resolve_env_settings() -> tuple[
    Literal["paper", "live-small", "live-scale"], Literal[0, 1, 2, 3]
]:
    """Resolve the env + phase_at_emit for audit writes.

    Dev environment maps to ``paper`` for audit_log purposes — the
    ``audit_log.env`` CHECK constraint doesn't include ``dev``; dev
    sessions still write to the paper-env chain.
    """
    from services.api.config import get_settings

    settings = get_settings()
    if settings.environment in ("paper", "live-small", "live-scale"):
        env: Literal["paper", "live-small", "live-scale"] = settings.environment
    else:
        env = "paper"  # dev → paper for audit purposes
    # Phase 1 default; Pivot-PR-D ships at Phase 1 onset post-pivot.
    return env, 1


@router.post(
    "/api/signals/{signal_id}/approve",
    tags=["signals"],
    status_code=200,
)
async def approve_signal(
    signal_id: UUID,
    body: SignalApproveRequest,
    repo: Phase1QueryRepo = Depends(_get_repo),
    session: SessionContext = Depends(get_session_context),
) -> dict[str, object]:
    """Approve a pending signal (Pivot-PR-D).

    Routes the approve action through ``services.risk.signal_dispatch``
    to write a ``signal_approved`` audit event + UPDATE the signal row
    to status='approved'. Order placement is NOT in PR-D scope; a
    follow-up consumes approved signals.
    """
    from services.api.db import get_session_factory
    from services.risk.signal_dispatch import (
        SignalDispatchError,
        apply_signal_dispatch,
        plan_signal_approve,
    )

    account_id = await _resolve_account_id(repo)
    env, phase = await _resolve_env_settings()
    plan = plan_signal_approve(
        signal_id=signal_id,
        account_id=account_id,
        decided_by_user_id=session.user_id,
        override_size=body.override_size,
    )
    try:
        result = await apply_signal_dispatch(
            plan,
            session_factory=get_session_factory(),  # module-level factory from db.py
            env=env,
            phase_at_emit=phase,
        )
    except SignalDispatchError as err:
        log.warning(
            "signal_approve_rejected",
            signal_id=str(signal_id),
            error_code=err.error_code,
        )
        status_code = (
            404
            if err.error_code == "SIGNAL_NOT_FOUND"
            else 409
            if err.error_code == "SIGNAL_NOT_PENDING"
            else 400
        )
        raise AppError(
            error_code=err.error_code,
            message=err.message,
            details=err.details,
            status_code=status_code,
        ) from err

    return {
        "signal_id": str(result.signal_id),
        "new_status": result.new_status,
        "audit_event_uuid": str(result.audit_event_uuid),
        "audit_sequence_no": result.audit_sequence_no,
        "intent_to_place_order": True,
    }


@router.post(
    "/api/signals/{signal_id}/reject",
    tags=["signals"],
    status_code=200,
)
async def reject_signal(
    signal_id: UUID,
    body: SignalRejectRequest,
    repo: Phase1QueryRepo = Depends(_get_repo),
    session: SessionContext = Depends(get_session_context),
) -> dict[str, object]:
    from services.api.db import get_session_factory
    from services.risk.signal_dispatch import (
        DecisionDiaryEntryInput,
        SignalDispatchError,
        apply_signal_dispatch,
        plan_signal_reject,
    )

    account_id = await _resolve_account_id(repo)
    env, phase = await _resolve_env_settings()
    diary = DecisionDiaryEntryInput(
        entry_class=body.decision_diary_entry.entry_class,
        tag=body.decision_diary_entry.tag,
        reasoning_text=body.decision_diary_entry.reasoning_text,
    )
    plan = plan_signal_reject(
        signal_id=signal_id,
        account_id=account_id,
        decided_by_user_id=session.user_id,
        diary_entry=diary,
    )
    try:
        result = await apply_signal_dispatch(
            plan,
            session_factory=get_session_factory(),
            env=env,
            phase_at_emit=phase,
        )
    except SignalDispatchError as err:
        status_code = (
            404
            if err.error_code == "SIGNAL_NOT_FOUND"
            else 409
            if err.error_code == "SIGNAL_NOT_PENDING"
            else 400
        )
        raise AppError(
            error_code=err.error_code,
            message=err.message,
            details=err.details,
            status_code=status_code,
        ) from err
    return {
        "signal_id": str(result.signal_id),
        "new_status": result.new_status,
        "audit_event_uuid": str(result.audit_event_uuid),
        "audit_sequence_no": result.audit_sequence_no,
    }


@router.post(
    "/api/signals/{signal_id}/defer",
    tags=["signals"],
    status_code=200,
)
async def defer_signal(
    signal_id: UUID,
    body: SignalDeferRequest,
    repo: Phase1QueryRepo = Depends(_get_repo),
    session: SessionContext = Depends(get_session_context),
) -> dict[str, object]:
    from services.api.db import get_session_factory
    from services.risk.signal_dispatch import (
        DecisionDiaryEntryInput,
        SignalDispatchError,
        apply_signal_dispatch,
        plan_signal_defer,
    )

    account_id = await _resolve_account_id(repo)
    env, phase = await _resolve_env_settings()
    diary = DecisionDiaryEntryInput(
        entry_class=body.decision_diary_entry.entry_class,
        tag=body.decision_diary_entry.tag,
        reasoning_text=body.decision_diary_entry.reasoning_text,
    )
    plan = plan_signal_defer(
        signal_id=signal_id,
        account_id=account_id,
        decided_by_user_id=session.user_id,
        diary_entry=diary,
    )
    try:
        result = await apply_signal_dispatch(
            plan,
            session_factory=get_session_factory(),
            env=env,
            phase_at_emit=phase,
        )
    except SignalDispatchError as err:
        status_code = (
            404
            if err.error_code == "SIGNAL_NOT_FOUND"
            else 409
            if err.error_code == "SIGNAL_NOT_PENDING"
            else 400
        )
        raise AppError(
            error_code=err.error_code,
            message=err.message,
            details=err.details,
            status_code=status_code,
        ) from err
    return {
        "signal_id": str(result.signal_id),
        "new_status": result.new_status,
        "audit_event_uuid": str(result.audit_event_uuid),
        "audit_sequence_no": result.audit_sequence_no,
    }


__all__ = ["router"]
