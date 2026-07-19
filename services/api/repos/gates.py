"""services/api/repos/gates.py — read surface for ``GET /api/system/gates``.

Strategy §10 acceptance-gate tracker (``Docs/crypto-perps-strategy.md``
§10): the C1→C2 scale-up gates A1-A4 (software correctness) and B1-B3
(strategy behavior) computed READ-ONLY from existing tables — fills,
orders, trades, signals, contracts, strategy_decisions, alerts,
strategy_worker_status, funding_rates. No writes, no schema changes, no
new enum values; the gate *classification* (green / red /
insufficient_data) is pure policy in ``services/api/schemas/gates.py`` —
this module only projects rows.

Evidence semantics, stated honestly (the schema layer repeats these in
the wire payload so the operator never over-reads a green):

* **A1** counts fills whose recorded price/fee/quantity are complete —
  the rows ARE the ingested venue fills (``broker_fill_id`` unique), so
  in-DB "matching" is completeness; the ±$1/±2% *statement* match is
  operator-run via ``scripts/operator_tools/reconcile_statement.py``.
* **A2** pairs each trade's entry fill with a ``stop_market`` orders row
  placed within 10 s (pairing is pure policy in the schema layer).
* **A3** has no dedicated restart table: the mechanical proxy is
  ``worker_failure`` alert days that PRECEDE the latest risk-loop
  heartbeat (death → process observably back). Manual drills per
  ``deploy/strategy_worker/README.md`` also count — operator-attested.
* **B1** per-fill slippage is computed against the linked signal's
  ``decision_price`` (the mark the sizing decision saw), signed with the
  calibration convention: positive = paid worse than decision mark.

A02 N/A — ``services/api/**`` is on the §2.3 hot-fix whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

#: Trailing-fill window fetched for A1/B1/B2 (B1/B2 slice the newest 20).
RECENT_FILLS_FETCH_LIMIT = 100

#: Funding comparison window for B3 (days).
FUNDING_WINDOW_DAYS = 30


@dataclass(frozen=True, slots=True)
class GateFillRow:
    """One fill joined to its order / signal / contract for gate math.

    ``decision_price`` (signal) and ``contract_size`` (contracts
    ``point_value``) are None when the linked rows are absent — the
    schema layer skips such fills for the affected metric rather than
    guessing.
    """

    filled_at_utc: datetime
    fill_price: Decimal
    fill_quantity: int
    commission: Decimal | None
    exchange_fee: Decimal | None
    direction: str
    order_type: str
    decision_price: Decimal | None
    contract_size: Decimal | None


@dataclass(frozen=True, slots=True)
class TradeOpenRow:
    """A position-opening event: the trade's entry fill + its contract."""

    trade_id: UUID
    contract_id: UUID | None
    entry_filled_at_utc: datetime


@dataclass(frozen=True, slots=True)
class StopOrderRow:
    """A ``stop_market`` orders row (the venue-resting protective stop)."""

    contract_id: UUID | None
    placed_at_utc: datetime
    status: str


@dataclass(frozen=True, slots=True)
class DecisionDayRow:
    """One ``strategy_decisions`` row projected to (date, status) for A4."""

    decision_date: date
    status: str


@dataclass(frozen=True, slots=True)
class FundingSourceStat:
    """Per-source funding aggregate over the trailing window (B3)."""

    source: str
    observation_count: int
    mean_abs_annualized_rate: Decimal | None


class GatesQueryRepo(Protocol):
    """The read surface the /api/system/gates handler depends on.

    Stubbed in unit tests (same pattern as ``CycleQueryRepo``).
    """

    async def fetch_recent_fills(self, account_id: UUID) -> list[GateFillRow]: ...

    async def fetch_trade_opens(self, account_id: UUID) -> list[TradeOpenRow]: ...

    async def fetch_stop_orders(self, account_id: UUID) -> list[StopOrderRow]: ...

    async def fetch_worker_failure_alert_times(self, account_id: UUID) -> list[datetime]: ...

    async def fetch_latest_heartbeat(self, account_id: UUID) -> datetime | None: ...

    async def fetch_decision_days(self, account_id: UUID) -> list[DecisionDayRow]: ...

    async def fetch_funding_source_stats(self) -> list[FundingSourceStat]: ...


def _dec(raw: object) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw))


class PostgresGatesQueryRepo:
    """Raw-SQL implementation over the request-scoped ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_recent_fills(self, account_id: UUID) -> list[GateFillRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT f.filled_at_utc, f.fill_price, f.fill_quantity,"
                    "       f.commission, f.exchange_fee, o.direction, o.order_type,"
                    "       s.decision_price, c.point_value AS contract_size "
                    "FROM fills f "
                    "JOIN orders o ON o.id = f.order_id "
                    "LEFT JOIN signals s ON s.id = o.signal_id "
                    "LEFT JOIN contracts c ON c.id = o.contract_id "
                    "WHERE f.account_id = :account_id "
                    "ORDER BY f.filled_at_utc DESC "
                    "LIMIT :n"
                ),
                {"account_id": account_id, "n": RECENT_FILLS_FETCH_LIMIT},
            )
        ).fetchall()
        out: list[GateFillRow] = []
        for row in rows:
            price = _dec(row.fill_price)
            if price is None or row.filled_at_utc is None:
                # A fill without a price cannot feed any gate metric.
                log.warning("gates_repo_fill_missing_price")
                continue
            out.append(
                GateFillRow(
                    filled_at_utc=row.filled_at_utc,
                    fill_price=price,
                    fill_quantity=int(row.fill_quantity or 0),
                    commission=_dec(row.commission),
                    exchange_fee=_dec(row.exchange_fee),
                    direction=str(row.direction),
                    order_type=str(row.order_type),
                    decision_price=_dec(row.decision_price),
                    contract_size=_dec(row.contract_size),
                )
            )
        return out

    async def fetch_trade_opens(self, account_id: UUID) -> list[TradeOpenRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT t.id AS trade_id, o.contract_id,"
                    "       MIN(f.filled_at_utc) AS entry_filled_at_utc "
                    "FROM trades t "
                    "JOIN orders o ON o.id = t.entry_order_id "
                    "JOIN fills f ON f.order_id = t.entry_order_id "
                    "WHERE t.account_id = :account_id "
                    "GROUP BY t.id, o.contract_id "
                    "ORDER BY MIN(f.filled_at_utc)"
                ),
                {"account_id": account_id},
            )
        ).fetchall()
        return [
            TradeOpenRow(
                trade_id=row.trade_id,
                contract_id=row.contract_id,
                entry_filled_at_utc=row.entry_filled_at_utc,
            )
            for row in rows
            if row.entry_filled_at_utc is not None
        ]

    async def fetch_stop_orders(self, account_id: UUID) -> list[StopOrderRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT contract_id, placed_at_utc, status "
                    "FROM orders "
                    "WHERE account_id = :account_id AND order_type = 'stop_market' "
                    "ORDER BY placed_at_utc"
                ),
                {"account_id": account_id},
            )
        ).fetchall()
        return [
            StopOrderRow(
                contract_id=row.contract_id,
                placed_at_utc=row.placed_at_utc,
                status=str(row.status),
            )
            for row in rows
            if row.placed_at_utc is not None
        ]

    async def fetch_worker_failure_alert_times(self, account_id: UUID) -> list[datetime]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT fired_at_utc FROM alerts "
                    "WHERE account_id = :account_id AND category = 'worker_failure' "
                    "ORDER BY fired_at_utc"
                ),
                {"account_id": account_id},
            )
        ).fetchall()
        return [row.fired_at_utc for row in rows if row.fired_at_utc is not None]

    async def fetch_latest_heartbeat(self, account_id: UUID) -> datetime | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT risk_loop_heartbeat_utc FROM strategy_worker_status "
                    "WHERE account_id = :account_id"
                ),
                {"account_id": account_id},
            )
        ).fetchone()
        return row.risk_loop_heartbeat_utc if row is not None else None

    async def fetch_decision_days(self, account_id: UUID) -> list[DecisionDayRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT decision_date, status FROM strategy_decisions "
                    "WHERE account_id = :account_id "
                    "ORDER BY decision_date"
                ),
                {"account_id": account_id},
            )
        ).fetchall()
        return [
            DecisionDayRow(decision_date=row.decision_date, status=str(row.status)) for row in rows
        ]

    async def fetch_funding_source_stats(self) -> list[FundingSourceStat]:
        # Mean |annualized rate| per source over the trailing window.
        # NUMERIC arithmetic end-to-end ([A05]); interval_hours <= 0 rows
        # are excluded rather than dividing by zero.
        rows = (
            await self._session.execute(
                text(
                    "SELECT source, COUNT(*) AS n,"
                    "       AVG(ABS(rate_per_interval * (24 / interval_hours) * 365))"
                    "         AS mean_abs_annualized "
                    "FROM funding_rates "
                    "WHERE observed_at_utc >= now() - make_interval(days => :days) "
                    "  AND interval_hours > 0 "
                    "GROUP BY source"
                ),
                {"days": FUNDING_WINDOW_DAYS},
            )
        ).fetchall()
        return [
            FundingSourceStat(
                source=str(row.source),
                observation_count=int(row.n),
                mean_abs_annualized_rate=_dec(row.mean_abs_annualized),
            )
            for row in rows
        ]


__all__ = [
    "FUNDING_WINDOW_DAYS",
    "RECENT_FILLS_FETCH_LIMIT",
    "DecisionDayRow",
    "FundingSourceStat",
    "GateFillRow",
    "GatesQueryRepo",
    "PostgresGatesQueryRepo",
    "StopOrderRow",
    "TradeOpenRow",
]
