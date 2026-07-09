"""services/webhook_pusher/cycle_digest.py — §3.8 digest push planner.

Crypto-pivot delta spec §3.8, pusher-side half of the ``/cycle`` digest:
the :class:`~services.webhook_pusher.event_pushes.EventPushPlan`-typed
planner for the 00:10 UTC #daily-brief push. The EMBED BUILDER lives in
the dependency-neutral :mod:`services.discord_shared.cycle_digest` (see
that module's docstring for the container-image import-graph rationale —
the discord_bot image cannot import anything under
``services.webhook_pusher`` because this package's ``__init__`` eagerly
pulls the sqlalchemy-touching dispatcher).

:func:`plan_cycle_digest_push` implements the scheduled-push skip:
``None`` when no decision has EVER been recorded (fresh DB / worker
never deployed — the 00:10 push stays silent during build-out rather
than posting an empty brief every midnight). A decision that exists but
is MISSING for today IS pushed, flagged amber — that's exactly the state
worth announcing. The :mod:`~services.webhook_pusher.cycle_digest_scheduler`
consumes the plan and the fire-time constants below.

A02 N/A — ``services/webhook_pusher/**`` is on the §2.3 hot-fix
whitelist. [A06]: every datetime tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from services.discord_shared.cycle_digest import build_cycle_digest_embed
from services.webhook_pusher.event_pushes import EventChannel, EventPushPlan

#: §3.8 — the digest fires at 00:10 UTC, five minutes after the 00:05
#: daily decision (which itself has retry slack inside the worker).
DIGEST_FIRE_HOUR_UTC: Final[int] = 0
DIGEST_FIRE_MINUTE_UTC: Final[int] = 10


def plan_cycle_digest_push(
    payload: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> EventPushPlan | None:
    """Payload → #daily-brief push plan; None = skip (no decision ever).

    The skip keeps the channel silent until the worker's first decision
    lands (C0 build window); every state after that — including a MISSING
    decision for today — pushes, because silence would be
    indistinguishable from "all fine".
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware UTC; got naive datetime")
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        return None
    embed = build_cycle_digest_embed(payload, now_utc=now_utc)
    decision_date = str(decision.get("decision_date") or "unknown")
    fire_date = now_utc.astimezone(UTC).date().isoformat()
    return EventPushPlan(
        channel=EventChannel.DAILY_BRIEF,
        embed=embed,
        dedupe_key=f"cycle_digest:{fire_date}:{decision_date}",
    )


__all__ = [
    "DIGEST_FIRE_HOUR_UTC",
    "DIGEST_FIRE_MINUTE_UTC",
    "plan_cycle_digest_push",
]
