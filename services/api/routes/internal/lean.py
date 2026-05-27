"""services/api/routes/internal/lean.py — LEAN Local → backend POST endpoint.

Pivot-PR-A (post-pivot 2026-05-12). Receives signal events from the
`lean_local` Docker container running on the operator's VPS. Shared-bearer
auth via ``LeanAuthMiddleware`` (outermost in the middleware stack); CSRF
is bypassed for LEAN-authenticated requests via the
``request.state.is_lean_authenticated`` flag set by the middleware.

**Architectural context:** This endpoint replaces the pre-pivot QC
ObjectStore polling architecture (backend-spec §4.5.1 RETIRED). LEAN pushes
events directly to the backend instead of writing to QC's ObjectStore for
the backend to poll. See ``Docs/decisions-log.md`` 2026-05-12 entry
"Phase-1 architecture pivot" + backend-spec §1.2 (post-pivot).

**Event routing (post Worker-PR-4 2026-05-12):**

* ``lean_strategy_initialized`` — log + 202 (Pivot-PR-A heartbeat path).
* ``lean_cycle_heartbeat`` — log + 202 (Pivot-PR-A heartbeat path).
* ``signal_emitted`` — validate full payload, write audit_log row +
  INSERT signals row via ``services.qc_adapter.signal_ingestion.ingest_signal_emitted``
  (canonical pattern; reused across QC adapter + LEAN paths post-pivot
  since both produce backend-spec §3.30 SIGNAL_EMITTED events with
  identical schema). Returns 202 with the minted ``signal_id`` +
  ``audit_event_uuid`` so the operator can deep-link from /system page.

Heartbeat events do NOT write to audit_log (A22: don't flood the chain
with operational liveness pings). signal_emitted DOES write to audit_log
(it's a strategy / risk-relevant event with downstream approval flows).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, status

from services.api import db as api_db
from services.api.errors import AppError
from services.api.heartbeats import get_heartbeat_registry
from services.api.repos.phase1 import PostgresPhase1QueryRepo
from services.api.schemas.lean import LeanEventAccepted, LeanEventRequest
from services.api.sse import emit_sse
from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit
from services.qc_adapter.payloads import QCEvent
from services.qc_adapter.signal_ingestion import (
    SignalIngestError,
    ingest_signal_emitted,
)

log = structlog.get_logger()

router = APIRouter(prefix="/api/internal/lean", tags=["internal-lean"])


def _require_lean_authenticated(request: Request) -> None:
    """Hard gate: only ``request.state.is_lean_authenticated = True`` may proceed.

    LeanAuthMiddleware sets this flag when the bearer is valid. If the flag
    is False / missing, the request did not pass through with a valid LEAN
    bearer; either the middleware was bypassed (which should be impossible
    in production), or the request came from a different auth path (the
    Discord bot's bearer, the operator's session cookie, etc.).

    We respond with 403 (not 401) because: 401 means "auth header missing
    or malformed" — which is what BotAuthMiddleware already returned if the
    bearer was a bot bearer + bad token. 403 means "authenticated, but you
    don't have access to this endpoint." A web session would land here with
    auth_strength=strong + role=owner; we still reject because this endpoint
    is service-account-only.
    """
    if not getattr(request.state, "is_lean_authenticated", False):
        raise AppError(
            error_code="LEAN_AUTH_REQUIRED",
            message=(
                "This endpoint requires authentication via the LEAN Local "
                "shared bearer token (sops field lean.api_bearer_token)."
            ),
            status_code=status.HTTP_403_FORBIDDEN,
        )


@router.post(
    "/signals",
    response_model=LeanEventAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive a signal event from LEAN Local",
    description=(
        "Receive a signal event from LEAN Local. **Pivot-PR-A scope:** "
        "heartbeat events only (`lean_strategy_initialized`, "
        "`lean_cycle_heartbeat`). Signal-emit events are accepted by "
        "Pivot-PR-D when the dispatcher lands."
    ),
)
async def post_lean_signal(
    body: LeanEventRequest,
    request: Request,
    _: Annotated[None, Depends(_require_lean_authenticated)],
) -> LeanEventAccepted:
    """Validate auth + body, route by event_type, return 202 Accepted.

    The dependency ``_require_lean_authenticated`` runs BEFORE the body
    validation per FastAPI's dependency-resolution order — a request
    without the LEAN bearer is rejected 403 even if the body is well-formed.
    Body validation (Pydantic) runs second and returns 422 on shape errors.

    Routing matrix:
        event_type=lean_strategy_initialized → log + 202 (Pivot-PR-A)
        event_type=lean_cycle_heartbeat      → log + 202 (Pivot-PR-A)
        event_type=signal_emitted            → 400 LEAN_EVENT_TYPE_NOT_WIRED
                                               (Pivot-PR-D wires this)
    """
    received_at = datetime.now(tz=UTC)
    log_kwargs = {
        "event_type": body.event_type,
        "algorithm_id": body.algorithm_id,
        "source_ts_utc": body.ts_utc.isoformat(),
        "session_date_et": body.session_date_et,
        "equity_usd": str(body.equity_usd) if body.equity_usd is not None else None,
        "live_mode": body.live_mode,
    }

    if body.event_type in ("lean_strategy_initialized", "lean_cycle_heartbeat"):
        # Update the in-memory heartbeat registry so the operator can see
        # "LEAN last cycled at T" on the System page + so a follow-up PR
        # can fire an alert when no heartbeat has arrived in >26h.
        # Closes a silent-failure surface — pre-this, LEAN's cron stopping
        # produced no observable signal.
        try:
            registry = get_heartbeat_registry()
            await registry.record(
                "lean_cycle",
                source_clock_utc=body.ts_utc,
                received_at_utc=received_at,
            )
        except Exception:
            # Registry failure must NOT fail the heartbeat POST: this is a
            # passive observability surface, not a critical path. Log + drop.
            log.exception("lean_heartbeat_registry_record_failed", **log_kwargs)
        log.info("lean_event_received", **log_kwargs)
        return LeanEventAccepted(
            received_at_utc=received_at,
            event_type=body.event_type,
            accepted=True,
            note=("Pivot-PR-A scope: heartbeat logged. Signal dispatch wires in Pivot-PR-D."),
        )

    if body.event_type == "signal_emitted":
        # Worker-PR-4 (2026-05-12): wired end-to-end. Validate required
        # payload fields here rather than via a model_validator on
        # LeanEventRequest — Pydantic v2's model_validator surfaces
        # ValueError objects inside the validation error ctx, which
        # FastAPI's RequestValidationError handler fails to JSON-serialize
        # cleanly. A boundary check at the route layer produces a clean
        # canonical AppError envelope without ctx-serialization issues.
        #
        # PR-B (2026-05-26): required-field set differs by signal_type per
        # exit-pipeline-design.md §6.1. Entries need target_contracts; exits
        # need exit_reason + prior_position_direction + prior_position_quantity
        # and reject target_contracts as REQUIRED (dispatcher computes it).
        # See exit-pipeline-design.md §Q3 (dispatcher-side sizing).
        if body.signal_type == "exit":
            required: dict[str, object | None] = {
                "market": body.market,
                "direction": body.direction,
                "decision_price": body.decision_price,
                "sizing_trace": body.sizing_trace,
                "strategy_version": body.strategy_version,
                "exit_reason": body.exit_reason,
                "prior_position_direction": body.prior_position_direction,
                "prior_position_quantity": body.prior_position_quantity,
            }
        else:
            required = {
                "market": body.market,
                "direction": body.direction,
                "target_contracts": body.target_contracts,
                "decision_price": body.decision_price,
                "sizing_trace": body.sizing_trace,
                "strategy_version": body.strategy_version,
            }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            log.warning(
                "lean_signal_emitted_missing_fields",
                missing=missing,
                signal_type=body.signal_type,
                **log_kwargs,
            )
            raise AppError(
                error_code="LEAN_SIGNAL_PAYLOAD_INCOMPLETE",
                message=(
                    f"signal_emitted event_type (signal_type={body.signal_type!r}) "
                    f"requires fields: {missing}"
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                details={"missing": missing, "signal_type": body.signal_type},
            )

        # Cross-field invariants for exits.
        if body.signal_type == "exit":
            # The strategy emits direction='flat' as the sentinel "target
            # ending position is flat"; any other direction would tell the
            # dispatcher to OPEN a new position. Reject loudly to surface
            # an emitter bug rather than silently flow through.
            if body.direction != "flat":
                log.warning(
                    "lean_signal_emitted_exit_direction_not_flat",
                    direction=body.direction,
                    **log_kwargs,
                )
                raise AppError(
                    error_code="LEAN_SIGNAL_EXIT_DIRECTION_INVALID",
                    message=(
                        "signal_type='exit' requires direction='flat' (sentinel "
                        "for target ending position). The dispatcher computes "
                        "the actual buy/sell side from the prior position; the "
                        "emit-time direction must be 'flat'."
                    ),
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    details={"direction": body.direction},
                )
            # prior_position_direction must indicate a held position; flat
            # makes no sense for an exit and would surface a bug in the
            # emitter's position snapshot.
            if body.prior_position_direction == "flat":
                log.warning("lean_signal_emitted_exit_prior_flat", **log_kwargs)
                raise AppError(
                    error_code="LEAN_SIGNAL_EXIT_PRIOR_DIRECTION_INVALID",
                    message=(
                        "signal_type='exit' requires prior_position_direction "
                        "to be 'long' or 'short'; 'flat' implies nothing to "
                        "exit and indicates an emitter bug."
                    ),
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    details={"prior_position_direction": body.prior_position_direction},
                )
            # paired_entry_market is reversal-only.
            if body.paired_entry_market is not None and body.exit_reason != "reversal":
                log.warning(
                    "lean_signal_emitted_paired_entry_on_non_reversal",
                    exit_reason=body.exit_reason,
                    paired_entry_market=body.paired_entry_market,
                    **log_kwargs,
                )
                raise AppError(
                    error_code="LEAN_SIGNAL_EXIT_PAIRED_ENTRY_INVALID",
                    message=(
                        "paired_entry_market may only be set when "
                        "exit_reason='reversal' (design §Q1)."
                    ),
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    details={
                        "exit_reason": body.exit_reason,
                        "paired_entry_market": body.paired_entry_market,
                    },
                )

        # mypy narrowing — all required-set values are non-None after the
        # branch-specific gate above. target_contracts is only narrowed for
        # the entry branch; the exit branch defaults it to 0 below when
        # constructing the ingestion payload (the dispatcher computes the
        # actual close qty at place-order time).
        assert body.market is not None
        assert body.direction is not None
        assert body.decision_price is not None
        assert body.sizing_trace is not None
        assert body.strategy_version is not None
        if body.signal_type == "entry":
            assert body.target_contracts is not None
        else:
            assert body.exit_reason is not None
            assert body.prior_position_direction is not None
            assert body.prior_position_quantity is not None

        session_factory = api_db.get_session_factory()

        # Resolve active account for this env. Phase 0/1 has exactly one
        # active accounts row (single-operator).
        async with session_factory() as repo_session:
            repo = PostgresPhase1QueryRepo(repo_session)
            account_id = await repo.fetch_active_account_id()
        if account_id is None:
            log.error("lean_signal_emitted_no_active_account", **log_kwargs)
            raise AppError(
                error_code="NO_ACTIVE_ACCOUNT",
                message=(
                    "No active accounts row found. Run the account bootstrap "
                    "migration before accepting signal_emitted events."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        # Map LEAN's live_mode → audit Environment Literal. live_mode=False
        # means backtest — but our audit chain doesn't have a 'backtest'
        # env; the closest mapping is 'paper' (the env we tag dry-run
        # signals with so backtest verifications still walk the chain
        # cleanly). For live_mode=True we'd source from APISettings.environment;
        # Phase 1 is always 'paper' until the Week 8 cutover ceremony.
        env: Environment = "paper"
        phase_at_emit: PhaseAtEmit = 1

        # Adapt the LEAN body into a QCEvent so we can reuse the canonical
        # qc_adapter.signal_ingestion.ingest_signal_emitted pipeline. The
        # QCEvent shape predates the pivot but its payload-validation +
        # audit-first write + slippage-head bootstrap logic is shared between
        # both ingest paths post-pivot. sequence_no=0 is a placeholder:
        # qc_adapter uses it for cursor tracking against QC's ObjectStore;
        # the LEAN path has no equivalent cursor, so 0 is the sentinel for
        # "not from QC".
        #
        # PR-B (2026-05-26): forward signal_type + exit fields into the
        # payload. For exits, target_contracts defaults to 0 — the dispatcher
        # computes the actual close qty from a fresh positions_current read
        # at place-order time per design §Q3, but signal_ingestion still
        # needs an int for the signals row. 0 is the sentinel; the
        # dispatcher MUST treat signal_type='exit' as authoritative and
        # ignore target_contracts. Exit fields land in the audit payload +
        # signals row via ingest_signal_emitted (PR-B section 5 of the
        # ingestion file).
        signal_payload: dict[str, object] = {
            "market": body.market,
            "direction": body.direction,
            "target_contracts": body.target_contracts if body.target_contracts is not None else 0,
            "decision_price": str(body.decision_price),
            "sizing_trace": body.sizing_trace,
            "signal_type": body.signal_type,
        }
        if body.signal_type == "exit":
            signal_payload["exit_reason"] = body.exit_reason
            signal_payload["prior_position_direction"] = body.prior_position_direction
            signal_payload["prior_position_quantity"] = body.prior_position_quantity
            if body.paired_entry_market is not None:
                signal_payload["paired_entry_market"] = body.paired_entry_market
        synthesized_event = QCEvent(
            sequence_no=0,
            event_type=AuditEventType.SIGNAL_EMITTED.value,
            source_clock_ts=body.ts_utc,
            qc_algorithm_version=body.strategy_version,
            payload=signal_payload,
        )

        try:
            result = await ingest_signal_emitted(
                session_factory,
                account_id=account_id,
                env=env,
                phase_at_emit=phase_at_emit,
                event=synthesized_event,
            )
        except SignalIngestError as exc:
            log.warning(
                "lean_signal_emitted_invalid_payload",
                error=str(exc),
                **log_kwargs,
            )
            raise AppError(
                error_code="LEAN_SIGNAL_PAYLOAD_INVALID",
                message=f"signal_emitted payload validation failed: {exc}",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                details={"event_type": body.event_type},
            ) from exc

        log.info(
            "lean_signal_emitted_ingested",
            signal_id=str(result.signal_id),
            audit_event_uuid=str(result.audit_event_uuid),
            market=body.market,
            direction=body.direction,
            target_contracts=body.target_contracts,
            signal_type=body.signal_type,
            exit_reason=body.exit_reason,
            **log_kwargs,
        )

        # Best-effort SSE fan-out so the web /signals page + the
        # webhook_pusher SSE subscriber pick up the freshly-emitted
        # signal without polling. SSE failure must NOT fail the LEAN
        # POST — the audit row + signals row are already durable and
        # any reconnect-with-Last-Event-ID consumer will catch up via
        # the replay buffer.
        try:
            sse_payload: dict[str, object | None] = {
                "action": "emitted",
                "signal_id": str(result.signal_id),
                "market": body.market,
                "direction": body.direction,
                "target_contracts": body.target_contracts,
                "decision_price": str(body.decision_price),
                "emitted_at_utc": body.ts_utc.isoformat(),
                "environment": env,
                "strategy_version": body.strategy_version,
                "audit_event_uuid": str(result.audit_event_uuid),
                "signal_type": body.signal_type,
            }
            if body.signal_type == "exit":
                sse_payload["exit_reason"] = body.exit_reason
                sse_payload["prior_position_direction"] = body.prior_position_direction
                sse_payload["prior_position_quantity"] = body.prior_position_quantity
                sse_payload["paired_entry_market"] = body.paired_entry_market
            await emit_sse(
                "signal",
                sse_payload,
            )
        except Exception:
            log.exception(
                "lean_signal_emitted_sse_emit_failed",
                signal_id=str(result.signal_id),
            )

        return LeanEventAccepted(
            received_at_utc=received_at,
            event_type=body.event_type,
            accepted=True,
            note=(
                f"signal_emitted accepted; audit_event_uuid={result.audit_event_uuid} "
                f"signal_id={result.signal_id}. Approval flow at /signals."
            ),
            signal_id=str(result.signal_id),
            audit_event_uuid=str(result.audit_event_uuid),
        )

    # Should be unreachable due to LeanEventType Literal validation, but
    # FastAPI's Literal validation runs before the route handler — if any
    # path reaches here, it's a code bug (new event_type added without
    # updating routing). Surface the bug loudly.
    log.error("lean_event_unhandled_type", **log_kwargs)
    raise AppError(
        error_code="LEAN_EVENT_TYPE_UNHANDLED",
        message=(
            f"Internal routing error: event_type={body.event_type!r} is in the "
            "LeanEventType Literal but no handler is registered."
        ),
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        details={"event_type": body.event_type},
    )


__all__ = ["router"]
