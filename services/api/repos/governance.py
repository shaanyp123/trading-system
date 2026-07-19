"""services/api/repos/governance.py — period aggregates for governance reports.

Delta spec §3.8 (C2 deliverable): the monthly + quarterly governance
reports pushed to #reports need read-only period aggregates over
EXISTING tables — trades (count/P&L), fills (fees), funding_rates
(telemetry-level funding), cash_sweeps (sweep summary), capital_events.
This repo is the data half; classification/rendering lives in
``services/api/schemas/governance.py`` (pure) and the Discord embed in
``services/discord_shared/governance_digest.py`` (stdlib-only).

Read-only projections; zero writes; no [A02] path. Honesty note carried
through the payload: funding here is TELEMETRY (observed rates), not
realized funding payments — those live in the CFM statement and are
operator-reconciled via scripts/operator_tools/reconcile_statement.py
(gate A1 discipline).

[A05] — Decimal everywhere. [A06] — tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _dec0(value: Any) -> Decimal:
    parsed = _dec(value)
    return parsed if parsed is not None else Decimal(0)


@dataclass(frozen=True, slots=True)
class TradesPeriodStats:
    closed_count: int
    winners: int
    losers: int
    realized_pnl_usd: Decimal
    realized_commission_usd: Decimal


@dataclass(frozen=True, slots=True)
class FeesPeriodStats:
    fill_count: int
    commission_usd: Decimal
    exchange_fee_usd: Decimal


@dataclass(frozen=True, slots=True)
class FundingPeriodStat:
    source: str
    observation_count: int
    mean_abs_annualized_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class SweepsPeriodStats:
    completed_count: int
    to_yield_usd: Decimal
    to_margin_usd: Decimal
    failed_count: int


@dataclass(frozen=True, slots=True)
class CapitalEventRow:
    event_type: str
    amount_usd: Decimal
    threshold_met: bool
    effective_at_utc: datetime


class GovernanceQueryRepo(Protocol):
    """Read-only period-aggregate surface for the governance reports."""

    async def fetch_trades_stats(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> TradesPeriodStats: ...

    async def fetch_fees_stats(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> FeesPeriodStats: ...

    async def fetch_funding_stats(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> list[FundingPeriodStat]: ...

    async def fetch_sweeps_stats(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> SweepsPeriodStats: ...

    async def fetch_capital_events(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> list[CapitalEventRow]: ...


class PostgresGovernanceQueryRepo:
    """SQL projections over the existing tables. Period = [start, end)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_trades_stats(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> TradesPeriodStats:
        row = (
            await self._session.execute(
                text(
                    "SELECT COUNT(*) AS n,"
                    "       COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS winners,"
                    "       COUNT(*) FILTER (WHERE realized_pnl_usd < 0) AS losers,"
                    "       COALESCE(SUM(realized_pnl_usd), 0) AS pnl,"
                    "       COALESCE(SUM(realized_commission_usd), 0) AS commission "
                    "FROM trades "
                    "WHERE account_id = :acct AND closed_at_utc IS NOT NULL "
                    "  AND closed_at_utc >= :start AND closed_at_utc < :end"
                ),
                {"acct": account_id, "start": start_utc, "end": end_utc},
            )
        ).fetchone()
        if row is None:  # pragma: no cover - aggregates always return one row
            return TradesPeriodStats(0, 0, 0, Decimal(0), Decimal(0))
        return TradesPeriodStats(
            closed_count=int(row.n or 0),
            winners=int(row.winners or 0),
            losers=int(row.losers or 0),
            realized_pnl_usd=_dec0(row.pnl),
            realized_commission_usd=_dec0(row.commission),
        )

    async def fetch_fees_stats(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> FeesPeriodStats:
        row = (
            await self._session.execute(
                text(
                    "SELECT COUNT(*) AS n,"
                    "       COALESCE(SUM(commission), 0) AS commission,"
                    "       COALESCE(SUM(exchange_fee), 0) AS exchange_fee "
                    "FROM fills "
                    "WHERE account_id = :acct "
                    "  AND filled_at_utc >= :start AND filled_at_utc < :end"
                ),
                {"acct": account_id, "start": start_utc, "end": end_utc},
            )
        ).fetchone()
        if row is None:  # pragma: no cover
            return FeesPeriodStats(0, Decimal(0), Decimal(0))
        return FeesPeriodStats(
            fill_count=int(row.n or 0),
            commission_usd=_dec0(row.commission),
            exchange_fee_usd=_dec0(row.exchange_fee),
        )

    async def fetch_funding_stats(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> list[FundingPeriodStat]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT source, COUNT(*) AS n,"
                    "       AVG(ABS(rate_per_interval * (24 / interval_hours) * 365))"
                    "         AS mean_abs_annualized "
                    "FROM funding_rates "
                    "WHERE observed_at_utc >= :start AND observed_at_utc < :end "
                    "  AND interval_hours > 0 "
                    "GROUP BY source ORDER BY source"
                ),
                {"start": start_utc, "end": end_utc},
            )
        ).fetchall()
        return [
            FundingPeriodStat(
                source=str(row.source),
                observation_count=int(row.n),
                mean_abs_annualized_rate=_dec(row.mean_abs_annualized),
            )
            for row in rows
        ]

    async def fetch_sweeps_stats(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> SweepsPeriodStats:
        row = (
            await self._session.execute(
                text(
                    "SELECT COUNT(*) FILTER (WHERE status = 'completed') AS completed,"
                    "       COALESCE(SUM(amount_usd) FILTER ("
                    "           WHERE status = 'completed' AND direction = 'to_yield'), 0)"
                    "         AS to_yield,"
                    "       COALESCE(SUM(amount_usd) FILTER ("
                    "           WHERE status = 'completed' AND direction = 'to_margin'), 0)"
                    "         AS to_margin,"
                    "       COUNT(*) FILTER (WHERE status = 'failed') AS failed "
                    "FROM cash_sweeps "
                    "WHERE requested_at_utc >= :start AND requested_at_utc < :end"
                ),
                {"start": start_utc, "end": end_utc},
            )
        ).fetchone()
        if row is None:  # pragma: no cover
            return SweepsPeriodStats(0, Decimal(0), Decimal(0), 0)
        return SweepsPeriodStats(
            completed_count=int(row.completed or 0),
            to_yield_usd=_dec0(row.to_yield),
            to_margin_usd=_dec0(row.to_margin),
            failed_count=int(row.failed or 0),
        )

    async def fetch_capital_events(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> list[CapitalEventRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT event_type, amount_usd, threshold_met, effective_at_utc "
                    "FROM capital_events "
                    "WHERE account_id = :acct "
                    "  AND effective_at_utc >= :start AND effective_at_utc < :end "
                    "ORDER BY effective_at_utc"
                ),
                {"acct": account_id, "start": start_utc, "end": end_utc},
            )
        ).fetchall()
        return [
            CapitalEventRow(
                event_type=str(row.event_type),
                amount_usd=_dec0(row.amount_usd),
                threshold_met=bool(row.threshold_met),
                effective_at_utc=row.effective_at_utc,
            )
            for row in rows
        ]


__all__ = [
    "CapitalEventRow",
    "FeesPeriodStats",
    "FundingPeriodStat",
    "GovernanceQueryRepo",
    "PostgresGovernanceQueryRepo",
    "SweepsPeriodStats",
    "TradesPeriodStats",
]
