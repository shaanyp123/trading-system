"""services/api/routes/signals.py — `/api/signals*` read endpoints.

Backend-spec §4.1.2, post-crypto-pivot:

  * ``GET /api/signals?status=&limit=&cursor=`` — list signals.
  * ``GET /api/signals`` proximity surface (latest per market).

Crypto-pivot C0-B4 (delta spec §3.8/§3.9): the per-trade approval
endpoints (``POST /api/signals/:id/{approve,reject,defer}``) are
RETIRED — the system is announce-only by operator mandate; the §3.3
strategy worker acts on its own decisions and trades are announced in
Discord ``#fills``. ``services.risk.signal_dispatch`` died with them.
The ``signal_approved/rejected/deferred`` audit enum values remain in
the locked taxonomy (history stays readable; no enum migration).
"""

from __future__ import annotations

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
    SignalListResponse,
    SignalSummary,
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
