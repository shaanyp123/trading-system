"""services/api/schemas/governance.py — ``GET /api/system/governance-report``.

Response models for the §3.8 monthly/quarterly governance-report data
(period aggregates from ``services/api/repos/governance.py``). Pure
assembly — the report PERIOD semantics (which month/quarter, fire
windows, once-per-period) live in the webhook_pusher scheduler; this
endpoint just aggregates an explicit [period_start, period_end) window
so it is trivially testable and reusable (the operator can curl any
window).

All money/rates are Decimal-serialized strings per [A05]. Funding is
telemetry (observed rates), NOT realized funding payments — the
``funding_note`` field says so on every payload (evidence-honesty
discipline, same as the gates tracker).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from services.api.repos.governance import (
    CapitalEventRow,
    FeesPeriodStats,
    FundingPeriodStat,
    SweepsPeriodStats,
    TradesPeriodStats,
)

#: Carried on every payload so no consumer can mistake rate telemetry
#: for realized funding payments (those are statement-reconciled, A1).
FUNDING_TELEMETRY_NOTE = (
    "Funding figures are observed-rate telemetry (funding_rates), not "
    "realized funding payments; realized funding is reconciled against "
    "the CFM statement via reconcile_statement.py (gate A1)."
)


class GovernanceTrades(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    closed_count: int
    winners: int
    losers: int
    realized_pnl_usd: str
    realized_commission_usd: str


class GovernanceFees(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fill_count: int
    commission_usd: str
    exchange_fee_usd: str
    total_usd: str


class GovernanceFundingSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    observation_count: int
    mean_abs_annualized_rate: str | None


class GovernanceSweeps(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_count: int
    to_yield_usd: str
    to_margin_usd: str
    failed_count: int


class GovernanceCapitalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str
    amount_usd: str
    threshold_met: bool
    effective_at_utc: datetime


class GovernanceReportResponse(BaseModel):
    """``GET /api/system/governance-report`` envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_start_utc: date
    period_end_utc: date = Field(description="Exclusive end (first day AFTER the period).")
    trades: GovernanceTrades
    fees: GovernanceFees
    funding_sources: list[GovernanceFundingSource]
    funding_note: str
    sweeps: GovernanceSweeps
    capital_events: list[GovernanceCapitalEvent]
    server_now: datetime


def build_governance_report_response(
    *,
    period_start: date,
    period_end: date,
    trades: TradesPeriodStats,
    fees: FeesPeriodStats,
    funding: list[FundingPeriodStat],
    sweeps: SweepsPeriodStats,
    capital_events: list[CapitalEventRow],
    now: datetime,
) -> GovernanceReportResponse:
    """Assemble the wire payload from the repo projections (pure)."""
    return GovernanceReportResponse(
        period_start_utc=period_start,
        period_end_utc=period_end,
        trades=GovernanceTrades(
            closed_count=trades.closed_count,
            winners=trades.winners,
            losers=trades.losers,
            realized_pnl_usd=str(trades.realized_pnl_usd),
            realized_commission_usd=str(trades.realized_commission_usd),
        ),
        fees=GovernanceFees(
            fill_count=fees.fill_count,
            commission_usd=str(fees.commission_usd),
            exchange_fee_usd=str(fees.exchange_fee_usd),
            total_usd=str(fees.commission_usd + fees.exchange_fee_usd),
        ),
        funding_sources=[
            GovernanceFundingSource(
                source=f.source,
                observation_count=f.observation_count,
                mean_abs_annualized_rate=(
                    str(f.mean_abs_annualized_rate)
                    if f.mean_abs_annualized_rate is not None
                    else None
                ),
            )
            for f in funding
        ],
        funding_note=FUNDING_TELEMETRY_NOTE,
        sweeps=GovernanceSweeps(
            completed_count=sweeps.completed_count,
            to_yield_usd=str(sweeps.to_yield_usd),
            to_margin_usd=str(sweeps.to_margin_usd),
            failed_count=sweeps.failed_count,
        ),
        capital_events=[
            GovernanceCapitalEvent(
                event_type=e.event_type,
                amount_usd=str(e.amount_usd),
                threshold_met=e.threshold_met,
                effective_at_utc=e.effective_at_utc,
            )
            for e in capital_events
        ],
        server_now=now,
    )


__all__ = [
    "FUNDING_TELEMETRY_NOTE",
    "GovernanceCapitalEvent",
    "GovernanceFees",
    "GovernanceFundingSource",
    "GovernanceReportResponse",
    "GovernanceSweeps",
    "GovernanceTrades",
    "build_governance_report_response",
]
