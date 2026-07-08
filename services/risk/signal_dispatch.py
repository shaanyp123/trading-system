"""services/risk/signal_dispatch.py — risk-state dispatch gate.

Crypto-pivot C0-B4 (delta spec §3.8/§3.9): the per-trade approval
dispatcher this module was built for (Pivot-PR-D:
``plan_signal_{approve,reject,defer}`` + ``apply_signal_dispatch``
behind ``POST /api/signals/:id/*``) is RETIRED — the system is
announce-only by operator mandate. What survives is the venue-agnostic
risk-state gate the trading loop consults before dispatching anything:

* :data:`RISK_STATES_PERMITTING_DISPATCH` — NORMAL + CONVALESCENT
  permit dispatch; HALT_NEW rejects (backend-spec §2.5).
* :func:`fetch_current_risk_state` — read the is_current=TRUE
  ``risk_state`` row.

The §3.3 strategy worker is the post-pivot consumer (same halt-gate
discipline the IBKR order worker used). The ``signal_approved/
rejected/deferred`` audit enum values remain in the locked taxonomy —
history stays readable; no enum migration ([A04]).

**A02 BINDS** — `services/risk/**` is on the forbidden whitelist;
`risk-review-approved` required.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

log = structlog.get_logger()


#: Risk-state values that PERMIT new signal dispatch. ``NORMAL`` is the
#: steady-state. ``CONVALESCENT`` permits trading per backend-spec §2.5
#: (the system has graduated from HALT_NEW + is in a 5-clean-session
#: probation window; new signals continue to flow but at reduced size
#: which is a separate concern from this gate). ``HALT_NEW`` rejects.
RISK_STATES_PERMITTING_DISPATCH: Final[frozenset[str]] = frozenset({"NORMAL", "CONVALESCENT"})


async def fetch_current_risk_state(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
) -> str | None:
    """Read ``risk_state.state`` for the current is_current=TRUE row.

    Returns ``None`` if no current row exists. Phase 1 invariant: the
    bootstrap migration + the kill-switch transitions both maintain
    exactly one is_current=TRUE row per account; ``None`` is a
    degenerate state that surfaces a bug (e.g., fresh deploy without
    the bootstrap row, or a manual psql DELETE).

    The dispatch gate treats ``None`` as fail-open — if the schema
    invariant breaks, we don't want to lock the operator out of
    signal flow. The operator's response is to seed the row via psql
    or wait for the next kill-switch transition.

    Schema reference: alembic 0003 ``risk_state`` table; partial unique
    index ``risk_state_current ON (account_id, is_current) WHERE
    is_current = TRUE`` enforces the one-row-per-account invariant.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT state FROM risk_state WHERE account_id = :acct AND is_current = TRUE"),
                {"acct": account_id},
            )
        ).fetchone()
    if row is None:
        return None
    return str(row.state)


__all__ = [
    "RISK_STATES_PERMITTING_DISPATCH",
    "fetch_current_risk_state",
]
