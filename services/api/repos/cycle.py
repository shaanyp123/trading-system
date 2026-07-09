"""services/api/repos/cycle.py — read surface for ``GET /api/system/cycle``.

Crypto-pivot §3.7 (``Docs/crypto-pivot-delta-spec.md``): the daily-cycle
status projection that replaces the retired bar-sync status surface.
Two read-only queries against the C1 strategy-worker tables
(``alembic/versions/2026-07-09_strategy_worker_tables.py``):

* ``strategy_decisions`` — latest row for the active account (the 00:05
  UTC decision outcome: per-asset trend score → target → action → est.
  costs inside the ``outcome`` JSONB written by
  ``services/signal/strategy_worker.py``).
* ``strategy_worker_status`` — the mutable per-account heartbeat row the
  worker UPSERTs every 30 s risk-loop tick.

READ-ONLY consumers — no writes, no schema changes. The JSONB payload is
passed through as ``dict`` and parsed defensively by the schema mapper
(``services/api/schemas/cycle.py``); a malformed outcome degrades to an
empty per-asset list, never a 500.

A02 N/A — ``services/api/**`` is on the §2.3 hot-fix whitelist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class StrategyDecisionRow:
    """Latest ``strategy_decisions`` row projected for the response model.

    Money/ratio columns surface as Decimal (None when the row is a skip —
    e.g. ``skipped_risk_state`` writes NULL equity). The ``outcome`` dict
    is the worker's ``strategy_decision_v1`` JSONB verbatim.
    """

    decision_date: date
    decided_at_utc: datetime
    status: str
    env: str
    equity_usd: Decimal | None
    equity_for_sizing_usd: Decimal | None
    v_target: Decimal | None
    m_combined: Decimal | None
    weekly_halved: bool
    outcome: dict[str, Any]
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class WorkerStatusRow:
    """``strategy_worker_status`` row (one per account, UPSERTed each tick)."""

    risk_loop_heartbeat_utc: datetime | None
    risk_loop_tick_count: int
    marks_stale: bool
    last_decision_date: date | None
    updated_at_utc: datetime


class CycleQueryRepo(Protocol):
    """The read surface the /api/system/cycle handler depends on.

    Stubbed in unit tests (same pattern as ``Phase1QueryRepo``).
    """

    async def fetch_latest_decision(self, account_id: UUID) -> StrategyDecisionRow | None: ...

    async def fetch_worker_status(self, account_id: UUID) -> WorkerStatusRow | None: ...


def _coerce_outcome(raw: Any) -> dict[str, Any]:
    """Normalize the JSONB column to a dict.

    asyncpg returns JSONB as ``dict`` already; a driver returning the
    serialized string (or a corrupt non-object payload) degrades to ``{}``
    with a structured warning rather than a 500.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            log.warning("cycle_repo_outcome_unparseable")
            return {}
        if isinstance(parsed, dict):
            return parsed
    log.warning("cycle_repo_outcome_not_object", outcome_type=type(raw).__name__)
    return {}


class PostgresCycleQueryRepo:
    """Raw-SQL implementation over the request-scoped ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_latest_decision(self, account_id: UUID) -> StrategyDecisionRow | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT decision_date, decided_at_utc, status, env, equity_usd,"
                    "       equity_for_sizing_usd, v_target, m_combined, weekly_halved,"
                    "       outcome, updated_at_utc "
                    "FROM strategy_decisions "
                    "WHERE account_id = :account_id "
                    "ORDER BY decision_date DESC "
                    "LIMIT 1"
                ),
                {"account_id": account_id},
            )
        ).fetchone()
        if row is None:
            return None
        return StrategyDecisionRow(
            decision_date=row.decision_date,
            decided_at_utc=row.decided_at_utc,
            status=row.status,
            env=row.env,
            equity_usd=row.equity_usd,
            equity_for_sizing_usd=row.equity_for_sizing_usd,
            v_target=row.v_target,
            m_combined=row.m_combined,
            weekly_halved=row.weekly_halved,
            outcome=_coerce_outcome(row.outcome),
            updated_at_utc=row.updated_at_utc,
        )

    async def fetch_worker_status(self, account_id: UUID) -> WorkerStatusRow | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT risk_loop_heartbeat_utc, risk_loop_tick_count, marks_stale,"
                    "       last_decision_date, updated_at_utc "
                    "FROM strategy_worker_status "
                    "WHERE account_id = :account_id"
                ),
                {"account_id": account_id},
            )
        ).fetchone()
        if row is None:
            return None
        return WorkerStatusRow(
            risk_loop_heartbeat_utc=row.risk_loop_heartbeat_utc,
            risk_loop_tick_count=row.risk_loop_tick_count,
            marks_stale=row.marks_stale,
            last_decision_date=row.last_decision_date,
            updated_at_utc=row.updated_at_utc,
        )


__all__ = [
    "CycleQueryRepo",
    "PostgresCycleQueryRepo",
    "StrategyDecisionRow",
    "WorkerStatusRow",
]
