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

from datetime import UTC, datetime
from typing import Any, Final, Literal
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
from services.api.repos.signal_proximity import fetch_latest_per_market
from services.api.routes._pagination import clamp_limit
from services.api.schemas.signal_proximity import (
    SignalProximityResponse,
    row_to_view,
)
from services.api.schemas.signals import (
    SignalApproveRequest,
    SignalDeferRequest,
    SignalListResponse,
    SignalRejectRequest,
    SignalSummary,
)
from services.api.session import SessionContext, get_session_context
from services.api.sse import emit_sse

log = structlog.get_logger()

router = APIRouter()


async def _emit_signal_decision_sse(
    *,
    action: Literal["approved", "rejected", "deferred"],
    signal_id: UUID,
    summary: dict[str, Any] | None,
    decided_by_user_id: str,
    decided_at_utc: datetime,
    env: str,
    diary_reasoning_text: str | None,
    audit_event_uuid: str,
) -> None:
    """Best-effort SSE emit for an approve/reject/defer decision.

    Failures are logged but do NOT propagate — the audit row + signals
    row updates are durable independent of the SSE fan-out. SSE is a
    notification surface; consumers reconnect with Last-Event-ID +
    catch up from the replay buffer when they miss a frame.

    Data shape matches
    ``services.webhook_pusher.sse_subscriber._route_signal_event``'s
    ``SignalDecisionEvent`` consumer plus a few audit-trace fields the
    web UI's ``useSignals`` invalidator reads.
    """
    if summary is None:
        log.warning(
            "signal_decision_sse_emit_skipped_no_summary",
            signal_id=str(signal_id),
            action=action,
        )
        return
    payload: dict[str, Any] = {
        "action": action,
        "signal_id": str(signal_id),
        "market": str(summary.get("market", "")),
        "direction": str(summary.get("direction", "")),
        "decided_at_utc": decided_at_utc.isoformat(),
        "decided_by_user_id": decided_by_user_id,
        "environment": env,
        "audit_event_uuid": audit_event_uuid,
    }
    if diary_reasoning_text is not None:
        payload["diary_reasoning_text"] = diary_reasoning_text
    try:
        sequence_no = await emit_sse("signal", payload)
    except Exception:
        log.exception(
            "signal_decision_sse_emit_failed",
            signal_id=str(signal_id),
            action=action,
        )
        return
    log.info(
        "signal_decision_sse_emitted",
        signal_id=str(signal_id),
        action=action,
        sse_sequence_no=sequence_no,
    )


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

    rows, next_cursor, has_more = await repo.fetch_signals_page(
        account_id,
        status=status,
        cursor=cursor,
        limit=clamp_limit(limit),
    )
    # 2026-05-28 fix: previously this returned items=[] unconditionally. The
    # original Day-15 comment noted "the row-to-SignalSummary mapper lands when
    # the dispatcher PR (Week 4 Wed) wires signal emission" — the dispatcher
    # PR shipped (signals ARE emitted nightly by LEAN now), but the mapper
    # never landed. Tonight (2026-05-28 21:30 UTC) the first real signal that
    # required UI interaction surfaced the gap. See `Docs/decisions-log.md`
    # 2026-05-28 entry. The repo query at services/api/repos/phase1.py was
    # also updated to select the full SignalSummary projection +
    # COALESCE(expires_at_utc, emitted_at_utc + 24h) since the lean-ingest
    # path doesn't set the column.
    return SignalListResponse(
        items=[_row_to_signal_summary(r) for r in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


# Both signals table hashes are stored full-length (CHAR(40) for strategy_hash
# = SHA-1 hex; CHAR(64) for parameter_set_hash = SHA-256 hex). The frontend
# only renders a short prefix in the queued-signals tile (frontend-spec
# §2.2.2). The 8-char window is the git-short-hash convention — enough entropy
# (32 bits) that a session's 11 markets x Phase 1 universe never collide.
_SHORT_HASH_CHARS: Final[int] = 8


def _row_to_signal_summary(row: dict[str, Any]) -> SignalSummary:
    """Map a `signals` table row dict to the wire-shape Pydantic model.

    Field deltas vs. the raw row:
    * ``id``: Pydantic accepts the row's ``id`` UUID directly.
    * ``strategy_hash`` (40 chars) → ``strategy_short_hash`` (first 8 chars).
    * ``parameter_set_hash`` (64 chars) → ``parameter_set_short_hash``
      (first 8 chars).
    * ``anomaly_reasons``: Postgres returns the TEXT[] column as a Python
      ``list[str]``. Pydantic validates each entry against the
      ``SignalAnomalyReason`` Literal union — an unknown reason would 422.
      Phase 0 the column defaults to ``'{}'`` (empty list) on insert.
    * ``expires_at_utc``: the repo query COALESCEs this to ``emitted_at_utc +
      24h`` when the column is NULL, so the wire shape is always non-null
      and the frontend's ``string`` typing holds.
    * ``signal_type``: the free-TEXT ``signals.signal_type`` column
      (``'entry'`` / ``'exit'`` on paper). Defaulted to ``'entry'`` when
      absent for backwards-compat with pre-column projections.
    * ``prior_position_direction``: extracted by the repo query from the
      ``sizing_trace`` JSONB (``lean_naive_sizing.prior_position_direction``);
      ``None`` for entries / rows without the trace key. Lets the UI render
      "CLOSE · was long/short" for exit signals.
    All other fields pass through unchanged.
    """
    strategy_hash = str(row["strategy_hash"])
    parameter_set_hash = str(row["parameter_set_hash"])
    # Pydantic v2 validates Literal unions on construction so direction +
    # status pass through as ``Any``; mypy can't see the Literal narrowing
    # but the schema guarantees a 422 on any unknown value at runtime.
    return SignalSummary(
        id=row["id"],
        market=str(row["market"]),
        direction=row["direction"],
        # `.get` with the canonical "entry" default (mirrors the
        # approve/reject/defer handlers' `summary.get("signal_type",
        # "entry")`) so the mapper tolerates a row projection that predates
        # the signal_type/prior_position_direction columns. The real
        # fetch_signals_page query always supplies both.
        signal_type=str(row.get("signal_type") or "entry"),
        prior_position_direction=row.get("prior_position_direction"),
        target_contracts=int(row["target_contracts"]),
        decision_price=row["decision_price"],
        expected_fill_price=row["expected_fill_price"],
        expected_slippage_bps=row["expected_slippage_bps"],
        unsettled=bool(row["unsettled"]),
        anomaly_reasons=row["anomaly_reasons"] or [],
        status=row["status"],
        emitted_at_utc=row["emitted_at_utc"],
        expires_at_utc=row["expires_at_utc"],
        strategy_short_hash=strategy_hash[:_SHORT_HASH_CHARS],
        parameter_set_short_hash=parameter_set_hash[:_SHORT_HASH_CHARS],
    )


@router.get(
    "/api/signals/proximity",
    tags=["signals"],
    response_model=SignalProximityResponse,
)
async def list_signal_proximity(
    session: SessionContext = Depends(get_session_context),
    db: AsyncSession = Depends(get_session),
) -> SignalProximityResponse:
    """Return latest-per-market proximity for the /signals "Watching" view.

    PR-B of ``Docs/signal-proximity-design.md`` (signed off 2026-05-28).
    Daily-resolution data: each row is the most recent ``signal_proximity``
    entry for the named market, written by the LEAN cycle heartbeat
    handler (``services/api/routes/internal/lean.py``).

    **Auth.** Mirrors ``GET /api/signals`` — the same
    ``Depends(get_session_context)`` dependency enforces the operator
    session cookie / bearer. No special service-account or role check.

    **Empty-state contract.** When the table has no rows (pre-first-cycle
    or post-truncate), returns ``{"as_of_cycle_ts_utc": null, "markets": []}``
    so the frontend can render the "Waiting for first LEAN cycle today"
    empty state cleanly per design §7.3.

    The session variable is unused; binding it keeps the auth dependency
    in the function signature (FastAPI resolves dependencies for side
    effects even when the result isn't used).
    """
    _ = session  # auth enforced via the dependency above; result unused
    as_of, rows = await fetch_latest_per_market(db)
    return SignalProximityResponse(
        as_of_cycle_ts_utc=as_of,
        markets=[row_to_view(row) for row in rows],
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
        fetch_current_risk_state,
        plan_signal_approve,
    )

    account_id = await _resolve_account_id(repo)
    env, phase = await _resolve_env_settings()
    # Snapshot market+direction+signal_type BEFORE dispatch so the SSE
    # payload is complete even if a concurrent UPDATE flips fields after
    # dispatch + so the planner receives the discriminator for L5
    # HALT_NEW bypass. Bypassed cleanly when the signal doesn't exist —
    # the dispatcher surfaces SIGNAL_NOT_FOUND with a clearer error than
    # a partial SSE emit would.
    summary = await repo.fetch_signal_summary(account_id, signal_id)
    # PR-H: read the current risk_state BEFORE dispatch. If the system
    # is HALT_NEW, the dispatcher raises SIGNAL_BLOCKED_BY_HALT which
    # maps to HTTP 409 below. NORMAL + CONVALESCENT permit. Exit-pipeline
    # PR-C (2026-05-27): exit signals BYPASS HALT_NEW per L5; the
    # dispatcher reads ``plan.signal_type`` to gate accordingly.
    risk_state = await fetch_current_risk_state(get_session_factory(), account_id=account_id)
    decided_at_utc = datetime.now(tz=UTC)
    # Default to 'entry' when summary missing (signal-not-found path
    # already 404s below via the dispatcher's validator); the literal
    # narrow keeps mypy strict happy.
    signal_type_raw = str(summary.get("signal_type", "entry")) if summary else "entry"
    signal_type: Literal["entry", "exit"] = "exit" if signal_type_raw == "exit" else "entry"
    plan = plan_signal_approve(
        signal_id=signal_id,
        account_id=account_id,
        decided_by_user_id=session.user_id,
        override_size=body.override_size,
        decided_at_utc=decided_at_utc,
        signal_type=signal_type,
    )
    try:
        result = await apply_signal_dispatch(
            plan,
            session_factory=get_session_factory(),  # module-level factory from db.py
            env=env,
            phase_at_emit=phase,
            current_risk_state=risk_state,
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
            if err.error_code in ("SIGNAL_NOT_PENDING", "SIGNAL_BLOCKED_BY_HALT")
            else 400
        )
        raise AppError(
            error_code=err.error_code,
            message=err.message,
            details=err.details,
            status_code=status_code,
        ) from err

    await _emit_signal_decision_sse(
        action="approved",
        signal_id=result.signal_id,
        summary=summary,
        decided_by_user_id=session.user_id,
        decided_at_utc=decided_at_utc,
        env=env,
        diary_reasoning_text=None,
        audit_event_uuid=str(result.audit_event_uuid),
    )

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
    summary = await repo.fetch_signal_summary(account_id, signal_id)
    decided_at_utc = datetime.now(tz=UTC)
    diary = DecisionDiaryEntryInput(
        entry_class=body.decision_diary_entry.entry_class,
        tag=body.decision_diary_entry.tag,
        reasoning_text=body.decision_diary_entry.reasoning_text,
    )
    # Exit-pipeline PR-C (2026-05-27): plumb signal_type for forensic
    # audit visibility. Reject is not gated by HALT_NEW; the
    # discriminator just flows into the audit payload.
    signal_type_raw = str(summary.get("signal_type", "entry")) if summary else "entry"
    signal_type: Literal["entry", "exit"] = "exit" if signal_type_raw == "exit" else "entry"
    plan = plan_signal_reject(
        signal_id=signal_id,
        account_id=account_id,
        decided_by_user_id=session.user_id,
        diary_entry=diary,
        decided_at_utc=decided_at_utc,
        signal_type=signal_type,
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
    await _emit_signal_decision_sse(
        action="rejected",
        signal_id=result.signal_id,
        summary=summary,
        decided_by_user_id=session.user_id,
        decided_at_utc=decided_at_utc,
        env=env,
        diary_reasoning_text=body.decision_diary_entry.reasoning_text,
        audit_event_uuid=str(result.audit_event_uuid),
    )
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
    summary = await repo.fetch_signal_summary(account_id, signal_id)
    decided_at_utc = datetime.now(tz=UTC)
    diary = DecisionDiaryEntryInput(
        entry_class=body.decision_diary_entry.entry_class,
        tag=body.decision_diary_entry.tag,
        reasoning_text=body.decision_diary_entry.reasoning_text,
    )
    # Exit-pipeline PR-C (2026-05-27): plumb signal_type — defer is not
    # HALT_NEW-gated; discriminator flows into the audit payload.
    signal_type_raw = str(summary.get("signal_type", "entry")) if summary else "entry"
    signal_type: Literal["entry", "exit"] = "exit" if signal_type_raw == "exit" else "entry"
    plan = plan_signal_defer(
        signal_id=signal_id,
        account_id=account_id,
        decided_by_user_id=session.user_id,
        diary_entry=diary,
        decided_at_utc=decided_at_utc,
        signal_type=signal_type,
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
    await _emit_signal_decision_sse(
        action="deferred",
        signal_id=result.signal_id,
        summary=summary,
        decided_by_user_id=session.user_id,
        decided_at_utc=decided_at_utc,
        env=env,
        diary_reasoning_text=body.decision_diary_entry.reasoning_text,
        audit_event_uuid=str(result.audit_event_uuid),
    )
    return {
        "signal_id": str(result.signal_id),
        "new_status": result.new_status,
        "audit_event_uuid": str(result.audit_event_uuid),
        "audit_sequence_no": result.audit_sequence_no,
    }


__all__ = ["router"]
