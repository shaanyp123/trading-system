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

**Pivot-PR-A scope:** the heartbeat path only. LEAN's `v1_strategy.py`
emits ``lean_strategy_initialized`` at algorithm initialize and
``lean_cycle_heartbeat`` once per 17:30 ET signal cycle. The endpoint
validates the bearer + body shape and logs structlog `lean_event_received`.

**NOT in Pivot-PR-A scope** (returns 400 ``LEAN_EVENT_TYPE_NOT_WIRED``):

* ``signal_emitted`` events. These land in Pivot-PR-D when the signal
  dispatcher (``services/risk/signal_dispatch.py``) is wired. Pivot-PR-D
  extends this endpoint to: validate the full signal payload, write a
  ``signal_emitted`` audit row via ``services.audit.writer.append_audit_event``,
  INSERT into the ``signals`` table, and emit an SSE ``signal_emitted``
  envelope. None of that is in scope here.

The endpoint deliberately writes NO audit_log row in Pivot-PR-A: the
heartbeat / init events are operational signals (LEAN is alive) not
strategy / risk / audit-relevant events. Anti-pattern A22 binds — never
write audit_log rows for unimportant boots / heartbeats.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, status

from services.api.errors import AppError
from services.api.schemas.lean import LeanEventAccepted, LeanEventRequest

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
        log.info("lean_event_received", **log_kwargs)
        return LeanEventAccepted(
            received_at_utc=received_at,
            event_type=body.event_type,
            accepted=True,
            note=("Pivot-PR-A scope: heartbeat logged. Signal dispatch wires in Pivot-PR-D."),
        )

    if body.event_type == "signal_emitted":
        # Future-PR scope. We log it as a separate event so a future
        # introspection of api logs makes the wire-but-not-implemented gap
        # explicit; this is the same pattern as the Day-15 501 stubs on
        # /api/signals/:id/{approve,reject,defer}.
        log.warning("lean_event_signal_emitted_not_wired", **log_kwargs)
        raise AppError(
            error_code="LEAN_EVENT_TYPE_NOT_WIRED",
            message=(
                "signal_emitted events are not yet accepted on this endpoint. "
                "Pivot-PR-D wires the dispatcher; until then LEAN should emit "
                "only heartbeat events. See Docs/decisions-log.md 2026-05-12."
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"event_type": body.event_type},
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
