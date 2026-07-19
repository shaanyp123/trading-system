"""Unit tests for ``GET /api/system/governance-report`` (§3.8 report data).

Route-level coverage via the conftest ASGI app + stubbed repos (same
pattern as ``test_api_gates_route.py``): happy-path aggregation with
[A05] string money, the fresh-DB (no account) empty shape, and the
period-validation 422s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient

from services.api.repos.governance import (
    CapitalEventRow,
    FeesPeriodStats,
    FundingPeriodStat,
    SweepsPeriodStats,
    TradesPeriodStats,
)
from services.api.routes import system as system_route


@dataclass
class _StubPhase1Repo:
    account_id: UUID | None = None

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account_id


@dataclass
class _StubGovernanceRepo:
    trades: TradesPeriodStats = field(
        default_factory=lambda: TradesPeriodStats(
            closed_count=4,
            winners=1,
            losers=3,
            realized_pnl_usd=Decimal("-53.5185"),
            realized_commission_usd=Decimal("3.20"),
        )
    )
    fees: FeesPeriodStats = field(
        default_factory=lambda: FeesPeriodStats(
            fill_count=9,
            commission_usd=Decimal("2.88"),
            exchange_fee_usd=Decimal("0.90"),
        )
    )
    funding: list[FundingPeriodStat] = field(
        default_factory=lambda: [
            FundingPeriodStat(
                source="binance_proxy",
                observation_count=93,
                mean_abs_annualized_rate=Decimal("0.07"),
            )
        ]
    )
    sweeps: SweepsPeriodStats = field(
        default_factory=lambda: SweepsPeriodStats(
            completed_count=0,
            to_yield_usd=Decimal("0"),
            to_margin_usd=Decimal("0"),
            failed_count=0,
        )
    )
    capital_events: list[CapitalEventRow] = field(
        default_factory=lambda: [
            CapitalEventRow(
                event_type="deposit",
                amount_usd=Decimal("802.10"),
                threshold_met=True,
                effective_at_utc=datetime(2026, 7, 18, 15, 30, tzinfo=UTC),
            )
        ]
    )
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)

    async def fetch_trades_stats(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> TradesPeriodStats:
        self.windows.append((start_utc, end_utc))
        return self.trades

    async def fetch_fees_stats(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> FeesPeriodStats:
        return self.fees

    async def fetch_funding_stats(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> list[FundingPeriodStat]:
        return self.funding

    async def fetch_sweeps_stats(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> SweepsPeriodStats:
        return self.sweeps

    async def fetch_capital_events(
        self, account_id: UUID, *, start_utc: datetime, end_utc: datetime
    ) -> list[CapitalEventRow]:
        return self.capital_events


def _wire(
    override_dep: Any,
    *,
    account_id: UUID | None,
    repo: _StubGovernanceRepo | None = None,
) -> _StubGovernanceRepo:
    governance_repo = repo or _StubGovernanceRepo()
    override_dep(system_route._get_repo, lambda: _StubPhase1Repo(account_id=account_id))
    override_dep(system_route._get_governance_repo, lambda: governance_repo)
    return governance_repo


_URL = "/api/system/governance-report?period_start=2026-07-01&period_end=2026-08-01"


class TestGovernanceReportRoute:
    async def test_happy_path_string_money(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        repo = _wire(override_dep, account_id=uuid4())
        resp = await api_client.get(_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["period_start_utc"] == "2026-07-01"
        assert body["period_end_utc"] == "2026-08-01"
        assert body["trades"]["realized_pnl_usd"] == "-53.5185"  # [A05] string
        assert body["fees"]["total_usd"] == "3.78"  # commission + exchange
        assert body["funding_sources"][0]["source"] == "binance_proxy"
        assert "telemetry" in body["funding_note"]
        assert body["sweeps"]["completed_count"] == 0
        assert body["capital_events"][0]["amount_usd"] == "802.10"
        # The query window is the UTC-midnight expansion of the dates.
        assert repo.windows == [
            (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
        ]

    async def test_no_account_returns_empty_account_scoped_sections(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, account_id=None)
        resp = await api_client.get(_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["trades"]["closed_count"] == 0
        assert body["capital_events"] == []
        # Venue-scoped surfaces still aggregate.
        assert body["funding_sources"][0]["source"] == "binance_proxy"

    async def test_inverted_period_422(self, api_client: AsyncClient, override_dep: Any) -> None:
        _wire(override_dep, account_id=uuid4())
        resp = await api_client.get(
            "/api/system/governance-report?period_start=2026-08-01&period_end=2026-07-01"
        )
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "INVALID_PERIOD"

    async def test_oversized_window_422(self, api_client: AsyncClient, override_dep: Any) -> None:
        _wire(override_dep, account_id=uuid4())
        resp = await api_client.get(
            "/api/system/governance-report?period_start=2024-01-01&period_end=2026-08-01"
        )
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "INVALID_PERIOD"
