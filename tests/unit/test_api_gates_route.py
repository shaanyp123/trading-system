"""Unit tests for ``GET /api/system/gates`` (strategy §10 gate tracker).

Route-level coverage via the conftest ASGI app + stubbed repos (same
pattern as ``test_api_cycle_route.py`` — no testcontainers), plus
pure-function coverage of the classification builder in
``services/api/schemas/gates.py``:

  * A1 consecutive-complete streak (green / insufficient / broken streak)
  * A2 stop-within-10s pairing + trigger-observed requirement
  * A3 crash-to-recovery proxy (alert before latest heartbeat, deduped
    per UTC day)
  * A4 clean-day streak (missed day / failed decision / worker_failure
    alert all break it; deliberate skips do not)
  * B1 trailing-20 slippage median/p90 vs 5/12 bps (stop fills excluded)
  * B2 fee bps over notional
  * B3 honest insufficient_data when no proxy funding source exists
  * scale_up_eligible = all green AND >= 45 days live
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient

from services.api.repos.gates import (
    DecisionDayRow,
    FundingSourceStat,
    GateFillRow,
    StopOrderRow,
    TradeOpenRow,
)
from services.api.routes import system as system_route
from services.api.schemas.gates import (
    build_system_gates_response,
    fill_slippage_bps,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


@dataclass
class _StubPhase1Repo:
    account_id: UUID | None = None

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account_id


@dataclass
class _StubGatesRepo:
    fills: list[GateFillRow] = field(default_factory=list)
    opens: list[TradeOpenRow] = field(default_factory=list)
    stops: list[StopOrderRow] = field(default_factory=list)
    failures: list[datetime] = field(default_factory=list)
    heartbeat: datetime | None = None
    decisions: list[DecisionDayRow] = field(default_factory=list)
    funding: list[FundingSourceStat] = field(default_factory=list)

    async def fetch_recent_fills(self, account_id: UUID) -> list[GateFillRow]:
        return self.fills

    async def fetch_trade_opens(self, account_id: UUID) -> list[TradeOpenRow]:
        return self.opens

    async def fetch_stop_orders(self, account_id: UUID) -> list[StopOrderRow]:
        return self.stops

    async def fetch_worker_failure_alert_times(self, account_id: UUID) -> list[datetime]:
        return self.failures

    async def fetch_latest_heartbeat(self, account_id: UUID) -> datetime | None:
        return self.heartbeat

    async def fetch_decision_days(self, account_id: UUID) -> list[DecisionDayRow]:
        return self.decisions

    async def fetch_funding_source_stats(self) -> list[FundingSourceStat]:
        return self.funding


def _fill(
    *,
    minutes_ago: int = 0,
    price: str = "64000",
    qty: int = 2,
    commission: str | None = "0.32",
    exchange_fee: str | None = "0.10",
    direction: str = "buy",
    order_type: str = "limit_marketable",
    decision_price: str | None = "64000",
    contract_size: str | None = "0.01",
) -> GateFillRow:
    return GateFillRow(
        filled_at_utc=NOW - timedelta(minutes=minutes_ago),
        fill_price=Decimal(price),
        fill_quantity=qty,
        commission=Decimal(commission) if commission is not None else None,
        exchange_fee=Decimal(exchange_fee) if exchange_fee is not None else None,
        direction=direction,
        order_type=order_type,
        decision_price=Decimal(decision_price) if decision_price is not None else None,
        contract_size=Decimal(contract_size) if contract_size is not None else None,
    )


def _gate(body: dict[str, Any], key: str) -> dict[str, Any]:
    gate = next((g for g in body["gates"] if g["key"] == key), None)
    assert gate is not None, f"gate {key} missing"
    return gate


def _wire(
    override_dep: Any,
    *,
    account_id: UUID | None,
    repo: _StubGatesRepo | None = None,
) -> _StubGatesRepo:
    gates_repo = repo or _StubGatesRepo()
    override_dep(system_route._get_repo, lambda: _StubPhase1Repo(account_id=account_id))
    override_dep(system_route._get_gates_repo, lambda: gates_repo)
    return gates_repo


class TestSystemGatesRoute:
    async def test_no_active_account_returns_empty_gates(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, account_id=None)
        resp = await api_client.get("/api/system/gates")
        assert resp.status_code == 200
        body = resp.json()
        assert {g["key"] for g in body["gates"]} == {"A1", "A2", "A3", "A4", "B1", "B2", "B3"}
        assert body["days_live"] is None
        assert body["all_mechanical_green"] is False
        assert body["scale_up_eligible"] is False

    async def test_populated_gates_render(self, api_client: AsyncClient, override_dep: Any) -> None:
        contract = uuid4()
        opened = NOW - timedelta(days=3)
        repo = _StubGatesRepo(
            fills=[_fill(minutes_ago=i) for i in range(25)],
            opens=[
                TradeOpenRow(trade_id=uuid4(), contract_id=contract, entry_filled_at_utc=opened)
            ],
            stops=[
                StopOrderRow(
                    contract_id=contract,
                    placed_at_utc=opened + timedelta(seconds=4),
                    status="filled",
                )
            ],
            heartbeat=NOW,
            # Anchor to the REAL clock: the route computes days_live from
            # datetime.now(), so a fixed date here goes stale overnight.
            decisions=[
                DecisionDayRow(
                    decision_date=datetime.now(tz=UTC).date() - timedelta(days=offset),
                    status="completed",
                )
                for offset in range(1, 10)
            ],
            funding=[
                FundingSourceStat(
                    source="coinbase_advanced",
                    observation_count=500,
                    mean_abs_annualized_rate=Decimal("0.08"),
                )
            ],
        )
        _wire(override_dep, account_id=uuid4(), repo=repo)
        resp = await api_client.get("/api/system/gates")
        assert resp.status_code == 200
        body = resp.json()
        assert _gate(body, "A1")["status"] == "green"
        assert _gate(body, "A2")["status"] == "insufficient_data"  # 1/10 opens
        assert "1/1 opens stop-covered" in _gate(body, "A2")["value"]
        assert _gate(body, "B1")["status"] == "green"
        assert _gate(body, "B2")["status"] == "green"
        assert _gate(body, "B3")["status"] == "insufficient_data"
        assert "no proxy-source rows" in _gate(body, "B3")["value"]
        assert body["days_live"] == 9
        assert body["scale_up_eligible"] is False

    async def test_b3_arms_green_with_proxy_rows(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """The binance_proxy logger's rows flip B3 out of insufficient_data
        with NO gate-code change (any non-coinbase source with a positive
        mean counts) — the PR-1 wiring proof, at route level."""
        repo = _StubGatesRepo(
            funding=[
                FundingSourceStat(
                    source="coinbase_advanced",
                    observation_count=500,
                    mean_abs_annualized_rate=Decimal("0.10"),
                ),
                FundingSourceStat(
                    source="binance_proxy",
                    observation_count=105,
                    mean_abs_annualized_rate=Decimal("0.08"),
                ),
            ]
        )
        _wire(override_dep, account_id=uuid4(), repo=repo)
        resp = await api_client.get("/api/system/gates")
        assert resp.status_code == 200
        b3 = _gate(resp.json(), "B3")
        assert b3["status"] == "green"
        assert "binance_proxy" in b3["value"]

    async def test_b3_goes_red_when_proxy_diverges_past_2x(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        repo = _StubGatesRepo(
            funding=[
                FundingSourceStat(
                    source="coinbase_advanced",
                    observation_count=500,
                    mean_abs_annualized_rate=Decimal("0.30"),
                ),
                FundingSourceStat(
                    source="binance_proxy",
                    observation_count=105,
                    mean_abs_annualized_rate=Decimal("0.10"),
                ),
            ]
        )
        _wire(override_dep, account_id=uuid4(), repo=repo)
        resp = await api_client.get("/api/system/gates")
        assert resp.status_code == 200
        b3 = _gate(resp.json(), "B3")
        assert b3["status"] == "red"
        assert "ratio 3.00" in b3["value"]


class TestGateClassification:
    """Pure builder coverage — no ASGI app."""

    def _build(self, **kwargs: Any) -> Any:
        defaults: dict[str, Any] = {
            "fills": [],
            "opens": [],
            "stops": [],
            "failure_times": [],
            "latest_heartbeat": None,
            "decisions": [],
            "funding_stats": [],
            "now": NOW,
        }
        defaults.update(kwargs)
        return build_system_gates_response(**defaults)

    def test_a1_streak_breaks_on_incomplete_fill(self) -> None:
        fills = [_fill(minutes_ago=i) for i in range(5)]
        fills.append(_fill(minutes_ago=6, commission=None, exchange_fee=None))
        fills.extend(_fill(minutes_ago=7 + i) for i in range(20))
        resp = self._build(fills=fills)
        a1 = next(g for g in resp.gates if g.key == "A1")
        assert a1.status == "red"  # >= 15 examined, streak only 5
        assert a1.value.startswith("5 consecutive")

    def test_a2_requires_trigger_observation(self) -> None:
        contract = uuid4()
        opens = [
            TradeOpenRow(
                trade_id=uuid4(),
                contract_id=contract,
                entry_filled_at_utc=NOW - timedelta(days=i),
            )
            for i in range(1, 12)
        ]
        stops = [
            StopOrderRow(
                contract_id=contract,
                placed_at_utc=o.entry_filled_at_utc + timedelta(seconds=9),
                status="working",
            )
            for o in opens
        ]
        resp = self._build(opens=opens, stops=stops)
        a2 = next(g for g in resp.gates if g.key == "A2")
        assert a2.status == "red"  # coverage met, no trigger observed
        assert "no stop trigger observed yet" in a2.value

        stops[0] = StopOrderRow(
            contract_id=contract, placed_at_utc=stops[0].placed_at_utc, status="filled"
        )
        resp = self._build(opens=opens, stops=stops)
        a2 = next(g for g in resp.gates if g.key == "A2")
        assert a2.status == "green"

    def test_a2_stop_outside_window_not_counted(self) -> None:
        contract = uuid4()
        opens = [
            TradeOpenRow(
                trade_id=uuid4(),
                contract_id=contract,
                entry_filled_at_utc=NOW - timedelta(days=1),
            )
        ]
        stops = [
            StopOrderRow(
                contract_id=contract,
                placed_at_utc=opens[0].entry_filled_at_utc + timedelta(seconds=11),
                status="working",
            )
        ]
        resp = self._build(opens=opens, stops=stops)
        a2 = next(g for g in resp.gates if g.key == "A2")
        assert "0/1 opens stop-covered" in a2.value

    def test_a3_recovery_proxy_dedupes_by_day(self) -> None:
        failures = [
            NOW - timedelta(days=2, hours=1),
            NOW - timedelta(days=2, hours=2),  # same UTC day — one recovery
            NOW - timedelta(days=5),
        ]
        resp = self._build(failure_times=failures, latest_heartbeat=NOW)
        a3 = next(g for g in resp.gates if g.key == "A3")
        assert a3.status == "green"
        assert a3.value.startswith("2 observed")

    def test_a3_no_heartbeat_counts_nothing(self) -> None:
        resp = self._build(failure_times=[NOW - timedelta(days=1)], latest_heartbeat=None)
        a3 = next(g for g in resp.gates if g.key == "A3")
        assert a3.value.startswith("0 observed")

    def test_a4_streak_counts_and_breaks(self) -> None:
        yesterday = NOW.date() - timedelta(days=1)
        decisions = [
            DecisionDayRow(decision_date=yesterday - timedelta(days=i), status="completed")
            for i in range(40)
        ]
        resp = self._build(decisions=decisions)
        a4 = next(g for g in resp.gates if g.key == "A4")
        assert a4.status == "green"
        assert a4.value.startswith("40 consecutive")

        # A failed decision 3 days back caps the streak at 3 clean days.
        decisions[3] = DecisionDayRow(decision_date=yesterday - timedelta(days=3), status="failed")
        resp = self._build(decisions=decisions)
        a4 = next(g for g in resp.gates if g.key == "A4")
        assert a4.value.startswith("3 consecutive")

    def test_a4_deliberate_skip_is_clean_but_missing_day_dirty(self) -> None:
        yesterday = NOW.date() - timedelta(days=1)
        decisions = [
            DecisionDayRow(decision_date=yesterday, status="skipped_risk_state"),
            DecisionDayRow(decision_date=yesterday - timedelta(days=1), status="completed"),
            # gap at days=2 (missed decision day)
            DecisionDayRow(decision_date=yesterday - timedelta(days=3), status="completed"),
        ]
        resp = self._build(decisions=decisions)
        a4 = next(g for g in resp.gates if g.key == "A4")
        assert a4.value.startswith("2 consecutive")

    def test_a4_worker_failure_alert_dirties_day(self) -> None:
        yesterday = NOW.date() - timedelta(days=1)
        decisions = [
            DecisionDayRow(decision_date=yesterday - timedelta(days=i), status="completed")
            for i in range(10)
        ]
        failure = datetime.combine(
            yesterday - timedelta(days=2), datetime.min.time(), tzinfo=UTC
        ) + timedelta(hours=3)
        resp = self._build(decisions=decisions, failure_times=[failure])
        a4 = next(g for g in resp.gates if g.key == "A4")
        assert a4.value.startswith("2 consecutive")

    def test_b1_red_on_wide_slippage_and_excludes_stops(self) -> None:
        # 20 buys filled 10 bps above decision price → median 10 > 5.
        fills = [_fill(minutes_ago=i, price="64064", decision_price="64000") for i in range(20)]
        resp = self._build(fills=fills)
        b1 = next(g for g in resp.gates if g.key == "B1")
        assert b1.status == "red"
        assert "median 10 bps" in b1.value

        # Stop fills are excluded — with only stops, B1 lacks data.
        stop_fills = [_fill(minutes_ago=i, order_type="stop_market") for i in range(20)]
        resp = self._build(fills=stop_fills)
        b1 = next(g for g in resp.gates if g.key == "B1")
        assert b1.status == "insufficient_data"

    def test_b2_fee_bps_math(self) -> None:
        # notional = 64000 * 2 * 0.01 = 1280; fees 0.42 → ~3.28 bps.
        resp = self._build(fills=[_fill(minutes_ago=i) for i in range(20)])
        b2 = next(g for g in resp.gates if g.key == "B2")
        assert b2.status == "green"
        assert "3.2812 bps" in b2.value

    def test_b3_ratio_against_proxy(self) -> None:
        stats = [
            FundingSourceStat(
                source="coinbase_advanced",
                observation_count=500,
                mean_abs_annualized_rate=Decimal("0.10"),
            ),
            FundingSourceStat(
                source="binance_proxy",
                observation_count=90,
                mean_abs_annualized_rate=Decimal("0.04"),
            ),
        ]
        resp = self._build(funding_stats=stats)
        b3 = next(g for g in resp.gates if g.key == "B3")
        assert b3.status == "red"  # ratio 2.5 > 2
        assert "ratio 2.50" in b3.value

        stats[1] = FundingSourceStat(
            source="binance_proxy",
            observation_count=90,
            mean_abs_annualized_rate=Decimal("0.08"),
        )
        resp = self._build(funding_stats=stats)
        b3 = next(g for g in resp.gates if g.key == "B3")
        assert b3.status == "green"

    def test_scale_up_requires_days_and_green(self) -> None:
        contract = uuid4()
        opened = NOW - timedelta(days=50)
        fills = [_fill(minutes_ago=i) for i in range(25)]
        opens = [
            TradeOpenRow(
                trade_id=uuid4(),
                contract_id=contract,
                entry_filled_at_utc=opened + timedelta(days=i),
            )
            for i in range(11)
        ]
        stops = [
            StopOrderRow(
                contract_id=contract,
                placed_at_utc=o.entry_filled_at_utc + timedelta(seconds=5),
                status="filled" if i == 0 else "working",
            )
            for i, o in enumerate(opens)
        ]
        yesterday = NOW.date() - timedelta(days=1)
        decisions = [
            DecisionDayRow(decision_date=yesterday - timedelta(days=i), status="completed")
            for i in range(50)
        ]
        failures = [NOW - timedelta(days=20), NOW - timedelta(days=40)]
        # Failures dirty their days — put them before the streak window…
        # actually a worker_failure 20 days ago breaks A4; use drills
        # older than the decision history instead.
        failures = [
            datetime.combine(yesterday - timedelta(days=60), datetime.min.time(), tzinfo=UTC),
            datetime.combine(yesterday - timedelta(days=61), datetime.min.time(), tzinfo=UTC),
        ]
        stats = [
            FundingSourceStat(
                source="coinbase_advanced",
                observation_count=500,
                mean_abs_annualized_rate=Decimal("0.08"),
            ),
            FundingSourceStat(
                source="binance_proxy",
                observation_count=90,
                mean_abs_annualized_rate=Decimal("0.08"),
            ),
        ]
        resp = self._build(
            fills=fills,
            opens=opens,
            stops=stops,
            failure_times=failures,
            latest_heartbeat=NOW,
            decisions=decisions,
            funding_stats=stats,
        )
        assert [g.status for g in resp.gates] == ["green"] * 7
        assert resp.all_mechanical_green is True
        assert resp.days_live == 50
        assert resp.scale_up_eligible is True


class TestFillSlippage:
    def test_sign_convention(self) -> None:
        buy_worse = _fill(price="64064", decision_price="64000", direction="buy")
        assert fill_slippage_bps(buy_worse) == Decimal("10")
        sell_worse = _fill(price="63936", decision_price="64000", direction="sell")
        assert fill_slippage_bps(sell_worse) == Decimal("10")
        buy_better = _fill(price="63936", decision_price="64000", direction="buy")
        assert fill_slippage_bps(buy_better) == Decimal("-10")

    def test_unusable_reference_returns_none(self) -> None:
        assert fill_slippage_bps(_fill(decision_price=None)) is None
        assert fill_slippage_bps(_fill(decision_price="0")) is None
        assert fill_slippage_bps(_fill(decision_price="NaN")) is None
