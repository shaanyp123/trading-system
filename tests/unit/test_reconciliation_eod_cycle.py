"""Unit tests for :mod:`services.reconciliation.eod_cycle`.

Pure-policy tests against the glue between
:func:`services.reconciliation.recon.plan_reconciliation_check`,
:func:`services.reconciliation.apply.apply_reconciliation_plan`, and
the FlexQuery fetcher. No testcontainers — the DB-touching path
(``build_backend_view``) is exercised at integration-test time when
the first live cycle lands; this file locks the orchestrator contract
+ the pure-policy view builders.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.reconciliation.eod_cycle import (
    EodCycleConfig,
    build_broker_view,
    make_cycle_callback,
    run_eod_cycle,
)
from services.reconciliation.flex_query_fetcher import (
    FlexAccountSummary,
    FlexCashBalance,
    FlexPosition,
    FlexQueryFetchError,
    ReconciliationSnapshot,
)
from services.reconciliation.recon import BrokerSource


def _build_snapshot(
    *,
    positions: tuple[FlexPosition, ...] = (),
    cash_balances: tuple[FlexCashBalance, ...] = (),
    nav: str = "100000.00",
) -> ReconciliationSnapshot:
    summary = FlexAccountSummary(
        account_id="DUQ825170",
        report_date=datetime(2026, 5, 12).date(),
        net_liquidation_usd=Decimal(nav),
        cash_usd=Decimal(0),
        stock_market_value_usd=Decimal(0),
        bond_market_value_usd=Decimal(0),
        futures_pnl_usd=Decimal(0),
    )
    return ReconciliationSnapshot(
        pulled_at_utc=datetime(2026, 5, 12, 22, 30, tzinfo=UTC),
        account_summary=summary,
        positions=positions,
        cash_balances=cash_balances,
    )


def _flex_pos(
    *,
    symbol: str = "MES",
    sec_type: str = "FUT",
    quantity: str = "1",
) -> FlexPosition:
    return FlexPosition(
        account_id="DUQ825170",
        symbol=symbol,
        sec_type=sec_type,
        quantity=Decimal(quantity),
        avg_cost_usd=Decimal("5230"),
        market_price_usd=Decimal("5234"),
        market_value_usd=Decimal("26170"),
        unrealized_pnl_usd=Decimal("20"),
    )


class TestEodCycleConfig:
    def test_shape_and_defaults(self) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=12345,
            flex_query_token="token-redacted",
        )
        assert config.phase_at_emit == 1

    def test_frozen(self) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        with pytest.raises(AttributeError):
            config.env = "live-small"  # type: ignore[misc]


class TestBuildBrokerView:
    def test_empty_snapshot_returns_empty_view(self) -> None:
        snap = _build_snapshot()
        view = build_broker_view(snap)
        assert view.positions == {}
        assert view.cash_usd == Decimal(0)
        assert view.source == BrokerSource.FLEXQUERY_EOD

    def test_futures_get_slash_prefix(self) -> None:
        snap = _build_snapshot(positions=(_flex_pos(symbol="MES", sec_type="FUT", quantity="2"),))
        view = build_broker_view(snap)
        assert view.positions == {"/MES": Decimal("2")}

    def test_etf_no_slash_prefix(self) -> None:
        snap = _build_snapshot(positions=(_flex_pos(symbol="TLT", sec_type="STK", quantity="100"),))
        view = build_broker_view(snap)
        assert view.positions == {"TLT": Decimal("100")}

    def test_zero_quantity_dropped(self) -> None:
        snap = _build_snapshot(
            positions=(
                _flex_pos(symbol="MES", quantity="1"),
                _flex_pos(symbol="ES", quantity="0"),
            )
        )
        view = build_broker_view(snap)
        assert view.positions == {"/MES": Decimal("1")}

    def test_same_market_sum_aggregates(self) -> None:
        # FlexQuery may report multiple rows per symbol (e.g., front +
        # next contract during a roll). Phase 1 backend aggregates by
        # market only, so the broker view must aggregate the same way.
        snap = _build_snapshot(
            positions=(
                _flex_pos(symbol="MES", quantity="1"),
                _flex_pos(symbol="MES", quantity="2"),
            )
        )
        view = build_broker_view(snap)
        assert view.positions == {"/MES": Decimal("3")}

    def test_usd_cash_summed(self) -> None:
        snap = _build_snapshot(
            cash_balances=(
                FlexCashBalance(currency="USD", balance=Decimal("10000")),
                FlexCashBalance(currency="USD", balance=Decimal("2500.50")),
                FlexCashBalance(currency="EUR", balance=Decimal("500")),
            )
        )
        view = build_broker_view(snap)
        assert view.cash_usd == Decimal("12500.50")  # non-USD excluded

    def test_short_position_negative_qty(self) -> None:
        snap = _build_snapshot(positions=(_flex_pos(symbol="MES", quantity="-1"),))
        view = build_broker_view(snap)
        assert view.positions == {"/MES": Decimal("-1")}


def _stub_session_factory(
    *, positions: list[dict[str, Any]], balance: dict[str, Any] | None
) -> Any:
    """Build a fake session_factory that returns the supplied snapshot rows."""

    def _make_row(d: dict[str, Any]) -> MagicMock:
        row = MagicMock()
        for k, v in d.items():
            setattr(row, k, v)
        return row

    session = MagicMock()

    async def execute(stmt: Any, params: Any) -> MagicMock:
        sql = str(stmt)
        result = MagicMock()
        if "positions_current" in sql:
            result.fetchall = MagicMock(return_value=[_make_row(p) for p in positions])
        elif "balances" in sql:
            result.fetchone = MagicMock(
                return_value=_make_row(balance) if balance is not None else None
            )
        return result

    session.execute = execute  # type: ignore[method-assign]

    @asynccontextmanager
    async def factory() -> Any:
        yield session

    return factory


class TestRunEodCycleOrchestrator:
    async def test_flex_fetch_failure_returns_none_no_db_writes(self) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        bad_client = MagicMock()
        bad_client.fetch_snapshot = AsyncMock(
            side_effect=FlexQueryFetchError(error_code="AUTH_INVALID", message="bad")
        )
        result = await run_eod_cycle(
            config=config,
            session_factory=_stub_session_factory(positions=[], balance=None),
            flex_client_factory=lambda: bad_client,
        )
        assert result is None

    async def test_happy_path_with_no_breaks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        # FlexQuery + backend agree exactly on positions + cash.
        snap = _build_snapshot(
            positions=(_flex_pos(symbol="MES", quantity="1"),),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)

        factory = _stub_session_factory(
            positions=[{"market": "/MES", "qty": 1}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        # Patch apply to capture the plan + dodge the audit writer's DB needs.
        captured: dict[str, Any] = {}

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            captured["kwargs"] = kwargs
            mock_result = MagicMock()
            mock_result.audit_event_uuids = ()
            mock_result.inserted_break_ids = ()
            mock_result.resolved_break_count = 0
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        result = await run_eod_cycle(
            config=config, session_factory=factory, flex_client_factory=lambda: client
        )
        assert result is not None
        # No breaks because backend and broker match exactly.
        assert len(captured["plan"].breaks_detected) == 0
        # check_passed audit event still emitted (per planner contract).
        assert len(captured["plan"].audit_events) == 1

    async def test_position_break_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        # Broker has 1 contract; backend has 2. Position tolerance is
        # exact-match per recon planner so this is an actionable break.
        snap = _build_snapshot(
            positions=(_flex_pos(symbol="MES", quantity="1"),),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)

        factory = _stub_session_factory(
            positions=[{"market": "/MES", "qty": 2}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        captured: dict[str, Any] = {}

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = True
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        await run_eod_cycle(
            config=config, session_factory=factory, flex_client_factory=lambda: client
        )
        assert len(captured["plan"].breaks_detected) == 1
        b = captured["plan"].breaks_detected[0]
        assert b.market == "/MES"
        assert b.expected == Decimal("2")
        assert b.actual == Decimal("1")
        assert b.delta == Decimal("1")  # |expected - actual|

    async def test_no_balance_row_treats_cash_as_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        snap = _build_snapshot(
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("5000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)
        factory = _stub_session_factory(positions=[], balance=None)

        captured: dict[str, Any] = {}

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        await run_eod_cycle(
            config=config, session_factory=factory, flex_client_factory=lambda: client
        )
        # Backend cash=0, broker cash=5000 → cash_usd break expected
        # (delta > tolerance since equity_baseline=0 means bps tolerance=0).
        plan = captured["plan"]
        assert any(b.metric.value == "cash_usd" for b in plan.breaks_detected)


class TestMakeCycleCallback:
    async def test_callback_invokes_run_eod_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        factory = _stub_session_factory(positions=[], balance=None)
        invoked = AsyncMock(return_value=None)
        monkeypatch.setattr("services.reconciliation.eod_cycle.run_eod_cycle", invoked)
        cb = make_cycle_callback(config=config, session_factory=factory)
        await cb(datetime(2026, 5, 12, tzinfo=UTC).date())
        invoked.assert_awaited_once()
        kwargs = invoked.await_args.kwargs  # type: ignore[union-attr]
        assert kwargs["config"] is config
        assert kwargs["session_factory"] is factory


class TestModuleContract:
    def test_all_exports_present(self) -> None:
        from services.reconciliation import eod_cycle as mod

        for name in mod.__all__:
            assert hasattr(mod, name), f"__all__ contains {name!r} but module lacks it"
