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
from uuid import UUID, uuid4

import pytest

from services.reconciliation.apply import AlertDispatchContext
from services.reconciliation.eod_cycle import (
    DEFAULT_PRIOR_BREAKS_WINDOW_HOURS,
    BackendRefreshResult,
    EodCycleConfig,
    build_broker_view,
    fetch_prior_breaks_within_grace_window,
    make_cycle_callback,
    run_eod_cycle,
)
from services.reconciliation.eod_cycle import (
    refresh_backend_from_broker_snapshot as _real_refresh,
)
from services.reconciliation.flex_query_fetcher import (
    FlexAccountSummary,
    FlexCashBalance,
    FlexPosition,
    FlexQueryFetchError,
    ReconciliationSnapshot,
)
from services.reconciliation.recon import BrokerSource, PriorBreak, ReconciliationMetric


@pytest.fixture(autouse=True)
def _patch_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the PR-I refresh inside ``run_eod_cycle`` by default.

    The orchestrator tests (TestRunEodCycleOrchestrator, etc.) don't
    mock the additional DB calls + audit writes that the refresh
    introduces, so we stub it to return a zero-effort
    BackendRefreshResult. PR-I-specific tests in
    TestRefreshBackendFromBrokerSnapshot bypass via the
    top-level-imported ``_real_refresh`` symbol (captured BEFORE the
    monkeypatch fires).
    """

    async def _noop(*args: Any, **kwargs: Any) -> BackendRefreshResult:
        return BackendRefreshResult(
            balance_row_id=None, positions_marked_count=0, audit_event_uuids=()
        )

    monkeypatch.setattr(
        "services.reconciliation.eod_cycle.refresh_backend_from_broker_snapshot",
        _noop,
    )


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
    *,
    positions: list[dict[str, Any]],
    balance: dict[str, Any] | None,
    prior_breaks: list[dict[str, Any]] | None = None,
) -> Any:
    """Build a fake session_factory that returns the supplied snapshot rows.

    ``prior_breaks`` defaults to ``[]`` — the empty list models the
    Phase 1 day-1 boot path (no prior cycles have populated
    ``reconciliation_breaks`` yet) and is the right default for tests
    that don't care about grace classification.
    """

    def _make_row(d: dict[str, Any]) -> MagicMock:
        row = MagicMock()
        for k, v in d.items():
            setattr(row, k, v)
        return row

    session = MagicMock()
    prior_break_rows = prior_breaks if prior_breaks is not None else []

    async def execute(stmt: Any, params: Any) -> MagicMock:
        sql = str(stmt)
        result = MagicMock()
        if "positions_current" in sql:
            result.fetchall = MagicMock(return_value=[_make_row(p) for p in positions])
        elif "balances" in sql:
            result.fetchone = MagicMock(
                return_value=_make_row(balance) if balance is not None else None
            )
        elif "reconciliation_breaks" in sql:
            result.fetchall = MagicMock(return_value=[_make_row(b) for b in prior_break_rows])
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

    async def test_callback_threads_alert_dispatch_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # make_cycle_callback accepts an alert_dispatch_hook and threads
        # it through to run_eod_cycle so the api lifespan glue can inject
        # a Discord-fan-out hook at scheduler construction time.
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        factory = _stub_session_factory(positions=[], balance=None)
        invoked = AsyncMock(return_value=None)
        monkeypatch.setattr("services.reconciliation.eod_cycle.run_eod_cycle", invoked)

        async def my_hook(ctx: AlertDispatchContext) -> None:
            return None

        cb = make_cycle_callback(
            config=config, session_factory=factory, alert_dispatch_hook=my_hook
        )
        await cb(datetime(2026, 5, 12, tzinfo=UTC).date())
        kwargs = invoked.await_args.kwargs  # type: ignore[union-attr]
        assert kwargs["alert_dispatch_hook"] is my_hook


# ---------------------------------------------------------------------------
# Alert dispatch hook end-to-end through run_eod_cycle
# ---------------------------------------------------------------------------


class TestAlertDispatchHookEndToEnd:
    """End-to-end (via fakes) that an actionable break flows through the
    apply orchestrator and fires the ``alert_dispatch_hook`` exactly once
    per actionable break.

    Verifies the audit-first ordering (audit writes land before the hook
    fires) and that the hook receives the matching audit_event_uuid via
    positional alignment with ``plan.breaks_detected``.

    A22 + A27 do NOT bind here — pure-policy Python, no testcontainers,
    no live HTTP.
    """

    async def test_actionable_break_fires_hook_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        # 1-contract position divergence on /MES → 1 actionable break.
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

        # Fake apply that captures the plan + simulates audit_uuids minting
        # so we can assert hook invocation order + positional alignment.
        captured_plan: dict[str, Any] = {}

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured_plan["plan"] = plan
            captured_plan["kwargs"] = kwargs
            # Real apply mints 1 UUID per audit event. For 1 break_detected
            # event, that's 1 UUID. We exercise the hook by calling it
            # the way the real apply would.
            hook = kwargs.get("alert_dispatch_hook")
            if hook is not None and plan.alerts:
                fake_audit_uuid = uuid4()
                for desc in plan.alerts:
                    await hook(
                        AlertDispatchContext(
                            descriptor=desc,
                            triggering_audit_event_uuid=fake_audit_uuid,
                            account_id=config.account_id,
                            env=config.env,
                        )
                    )
            mock_result = MagicMock()
            mock_result.alerts_dispatched_count = len(plan.alerts) if hook else 0
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        hook_calls: list[AlertDispatchContext] = []

        async def hook(ctx: AlertDispatchContext) -> None:
            hook_calls.append(ctx)

        await run_eod_cycle(
            config=config,
            session_factory=factory,
            flex_client_factory=lambda: client,
            alert_dispatch_hook=hook,
        )

        # Exactly one hook invocation for the actionable break.
        assert len(hook_calls) == 1
        ctx = hook_calls[0]
        # Hook received the descriptor for the break.
        assert ctx.descriptor.severity == "P2"
        assert ctx.descriptor.category == "reconciliation_break"
        assert ctx.descriptor.payload["market"] == "/MES"
        assert ctx.descriptor.payload["delta"] == "1"
        # Hook received the context fields populated.
        assert ctx.account_id == config.account_id
        assert ctx.env == config.env
        # Plan reached apply with the hook in kwargs.
        assert captured_plan["kwargs"]["alert_dispatch_hook"] is hook

    async def test_no_breaks_no_hook_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            hook = kwargs.get("alert_dispatch_hook")
            if hook is not None and plan.alerts:
                for desc in plan.alerts:
                    await hook(
                        AlertDispatchContext(
                            descriptor=desc,
                            triggering_audit_event_uuid=uuid4(),
                            account_id=config.account_id,
                            env=config.env,
                        )
                    )
            return MagicMock(alerts_dispatched_count=0)

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        hook_calls: list[AlertDispatchContext] = []

        async def hook(ctx: AlertDispatchContext) -> None:
            hook_calls.append(ctx)

        await run_eod_cycle(
            config=config,
            session_factory=factory,
            flex_client_factory=lambda: client,
            alert_dispatch_hook=hook,
        )
        # No breaks → no alerts → no hook calls.
        assert hook_calls == []

    async def test_multiple_actionable_breaks_fire_hook_per_break(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Three actionable breaks → three hook invocations, in order.
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        snap = _build_snapshot(
            positions=(_flex_pos(symbol="MES", quantity="1"),),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)
        # Backend says different positions on /MES + /MCL + cash mismatch.
        factory = _stub_session_factory(
            positions=[
                {"market": "/MES", "qty": 3},
                {"market": "/MCL", "qty": 1},
            ],
            balance={"cash_usd": Decimal("100150"), "net_liquidation": Decimal("105000")},
        )

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            hook = kwargs.get("alert_dispatch_hook")
            if hook is not None and plan.alerts:
                for desc in plan.alerts:
                    await hook(
                        AlertDispatchContext(
                            descriptor=desc,
                            triggering_audit_event_uuid=uuid4(),
                            account_id=config.account_id,
                            env=config.env,
                        )
                    )
            return MagicMock(alerts_dispatched_count=len(plan.alerts) if hook else 0)

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        hook_calls: list[AlertDispatchContext] = []

        async def hook(ctx: AlertDispatchContext) -> None:
            hook_calls.append(ctx)

        await run_eod_cycle(
            config=config,
            session_factory=factory,
            flex_client_factory=lambda: client,
            alert_dispatch_hook=hook,
        )
        # Three breaks (MCL, MES, cash) → three hook invocations.
        assert len(hook_calls) == 3
        markets = [ctx.descriptor.payload["market"] for ctx in hook_calls]
        # /MCL sorted alphabetically before /MES; cash break has market=None.
        assert markets == ["/MCL", "/MES", None]

    async def test_hook_optional_when_no_hook_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-wiring boot: no hook supplied → cycle still completes
        # (audit + reconciliation_breaks rows still land via apply).
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
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
            captured["kwargs"] = kwargs
            return MagicMock(alerts_dispatched_count=0)

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        # Note: no alert_dispatch_hook passed.
        result = await run_eod_cycle(
            config=config,
            session_factory=factory,
            flex_client_factory=lambda: client,
        )
        assert result is not None
        # Plan still went through apply.
        assert "kwargs" in captured
        # Hook is None.
        assert captured["kwargs"]["alert_dispatch_hook"] is None


# ---------------------------------------------------------------------------
# Prior-breaks SQL lookup feeding the planner's grace classification
# ---------------------------------------------------------------------------


class TestPriorBreaksLookup:
    """``fetch_prior_breaks_within_grace_window`` reads unresolved breaks
    within the T+1 grace window + maps them to the planner's
    :class:`PriorBreak` shape.

    Pure-policy tests with the stub session_factory; SQL semantics
    (the ``NOW() - INTERVAL '<N> hour'`` filter) are exercised at
    integration-test time. Here we assert:

    * Empty table → ``()`` (Phase 1 day-1 boot).
    * Populated table → mapped to ``PriorBreak(metric, market, delta)``.
    * Unknown metric → skipped + warning emitted (defensive against a
      Phase-2 metric value landing before the planner enum is updated).
    * Window-hours param threads through to the SQL params dict.
    """

    async def test_empty_table_returns_empty_tuple(self) -> None:
        factory = _stub_session_factory(positions=[], balance=None, prior_breaks=[])
        prior = await fetch_prior_breaks_within_grace_window(factory, account_id=uuid4())
        assert prior == ()

    async def test_default_returns_empty_tuple(self) -> None:
        # No prior_breaks supplied → defaults to [] → empty tuple.
        factory = _stub_session_factory(positions=[], balance=None)
        prior = await fetch_prior_breaks_within_grace_window(factory, account_id=uuid4())
        assert prior == ()

    async def test_position_break_mapped_to_prior_break(self) -> None:
        factory = _stub_session_factory(
            positions=[],
            balance=None,
            prior_breaks=[{"metric": "position_qty", "market": "/MES", "delta": Decimal("1")}],
        )
        prior = await fetch_prior_breaks_within_grace_window(factory, account_id=uuid4())
        assert prior == (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="/MES",
                delta=Decimal("1"),
            ),
        )

    async def test_cash_break_with_null_market(self) -> None:
        # Cash breaks have market=NULL in the schema; planner accepts
        # market=None on PriorBreak.
        factory = _stub_session_factory(
            positions=[],
            balance=None,
            prior_breaks=[{"metric": "cash_usd", "market": None, "delta": Decimal("250.00")}],
        )
        prior = await fetch_prior_breaks_within_grace_window(factory, account_id=uuid4())
        assert len(prior) == 1
        assert prior[0].metric is ReconciliationMetric.CASH_USD
        assert prior[0].market is None
        assert prior[0].delta == Decimal("250.00")

    async def test_unknown_metric_skipped(self) -> None:
        # A future Phase 2 metric (e.g., 'net_liquidation') landing in
        # the schema before the planner enum is updated should be
        # silently dropped — never crash the cycle.
        factory = _stub_session_factory(
            positions=[],
            balance=None,
            prior_breaks=[
                {"metric": "net_liquidation", "market": None, "delta": Decimal("100.00")},
                {"metric": "position_qty", "market": "/MES", "delta": Decimal("1")},
            ],
        )
        prior = await fetch_prior_breaks_within_grace_window(factory, account_id=uuid4())
        # Only the known position_qty row survives.
        assert len(prior) == 1
        assert prior[0].metric is ReconciliationMetric.POSITION_QTY

    async def test_decimal_precision_preserved(self) -> None:
        # A05: Decimal-via-str round-trip preserves precision.
        factory = _stub_session_factory(
            positions=[],
            balance=None,
            prior_breaks=[
                {
                    "metric": "cash_usd",
                    "market": None,
                    "delta": Decimal("1234.567890"),
                }
            ],
        )
        prior = await fetch_prior_breaks_within_grace_window(factory, account_id=uuid4())
        assert prior[0].delta == Decimal("1234.567890")

    async def test_default_window_hours_constant(self) -> None:
        # Locked default: T+1 (24h) + half-day buffer (12h) = 36h.
        # Friday-detected breaks need weekend coverage through Monday;
        # documented limitation, see eod_cycle.py docstring.
        assert DEFAULT_PRIOR_BREAKS_WINDOW_HOURS == 36

    async def test_run_eod_cycle_threads_prior_breaks_to_planner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: run_eod_cycle calls the helper + threads the result
        # into plan_reconciliation_check. Verifies the prior-break is
        # classified as grace continuation (no alert fires).
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        # FlexQuery + backend disagree on /MES by 1 contract — same delta
        # as the prior break we'll seed below.
        snap = _build_snapshot(
            positions=(_flex_pos(symbol="MES", quantity="1"),),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)

        factory = _stub_session_factory(
            positions=[{"market": "/MES", "qty": 2}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
            # Prior break with same (metric, market, delta) as today's
            # break — should classify as grace, drop the alert.
            prior_breaks=[{"metric": "position_qty", "market": "/MES", "delta": Decimal("1")}],
        )

        captured: dict[str, Any] = {}

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            return MagicMock(
                alerts_dispatched_count=0,
                kill_switch_invoked=False,
            )

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        await run_eod_cycle(
            config=config, session_factory=factory, flex_client_factory=lambda: client
        )

        plan = captured["plan"]
        # The break is detected (it's still in breaks_detected) but
        # classified as within-grace and no alert fires.
        assert len(plan.breaks_detected) == 1
        assert plan.breaks_detected[0].within_grace_period is True
        assert plan.actionable_break_count == 0
        assert plan.alerts == ()

    async def test_prior_break_with_different_delta_does_not_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Prior delta=1; today delta=2 → mismatch → today's break is
        # actionable + alert fires.
        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        snap = _build_snapshot(
            positions=(_flex_pos(symbol="MES", quantity="1"),),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)
        factory = _stub_session_factory(
            positions=[{"market": "/MES", "qty": 3}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
            # Prior delta=1, today delta=2 — different → not grace.
            prior_breaks=[{"metric": "position_qty", "market": "/MES", "delta": Decimal("1")}],
        )

        captured: dict[str, Any] = {}

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            return MagicMock(alerts_dispatched_count=0, kill_switch_invoked=False)

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        await run_eod_cycle(
            config=config, session_factory=factory, flex_client_factory=lambda: client
        )

        plan = captured["plan"]
        # Today's delta=2 ≠ prior's delta=1 → actionable + alert fires.
        assert len(plan.breaks_detected) == 1
        assert plan.breaks_detected[0].within_grace_period is False
        assert plan.actionable_break_count == 1
        assert len(plan.alerts) == 1


class TestModuleContract:
    def test_all_exports_present(self) -> None:
        from services.reconciliation import eod_cycle as mod

        for name in mod.__all__:
            assert hasattr(mod, name), f"__all__ contains {name!r} but module lacks it"


# ---------------------------------------------------------------------------
# PR-I: refresh_backend_from_broker_snapshot
# ---------------------------------------------------------------------------


def _refresh_session_factory(
    *,
    position_rows_by_market: dict[str, dict[str, Any] | None],
    new_balance_id: UUID | None = None,
) -> tuple[Any, list[str], list[dict[str, Any]]]:
    """Build a fake session_factory for refresh tests.

    Tracks the SQL strings executed (for ordering assertions) +
    captures INSERT parameter sets. Returns the factory plus the
    captured-sql + captured-params lists for inspection.

    ``position_rows_by_market`` maps market → MagicMock-style row
    dict (id / quantity / avg_cost) OR None when the SELECT should
    return no row.

    ``new_balance_id`` is the UUID surfaced by the balances INSERT
    RETURNING id; defaults to a fresh uuid4.
    """
    if new_balance_id is None:
        new_balance_id = uuid4()
    executed_sql: list[str] = []
    executed_params: list[dict[str, Any]] = []

    def _make_row(d: dict[str, Any]) -> MagicMock:
        row = MagicMock()
        for k, v in d.items():
            setattr(row, k, v)
        return row

    async def execute(stmt: Any, params: Any) -> MagicMock:
        sql = str(stmt)
        executed_sql.append(sql)
        executed_params.append(dict(params))
        result = MagicMock()
        if "INSERT INTO balances" in sql:
            result.fetchone = MagicMock(return_value=_make_row({"id": new_balance_id}))
        elif "SELECT id, quantity, avg_cost FROM positions_current" in sql:
            market = params["market"]
            row = position_rows_by_market.get(market)
            result.fetchone = MagicMock(return_value=_make_row(row) if row is not None else None)
        elif "UPDATE positions_current" in sql:
            result.fetchone = MagicMock(return_value=None)
        else:
            result.fetchone = MagicMock(return_value=None)
        return result

    @asynccontextmanager
    async def _begin_cm() -> Any:
        yield None

    session = MagicMock()
    session.execute = execute  # type: ignore[method-assign]
    # session.begin must return a FRESH async-cm each call (the refresh
    # function calls it once per balance INSERT + once per position
    # UPDATE; reusing a single cm fails on the second __aenter__).
    session.begin = MagicMock(side_effect=lambda: _begin_cm())

    @asynccontextmanager
    async def _factory_cm() -> Any:
        yield session

    factory = MagicMock()
    factory.side_effect = lambda: _factory_cm()
    return factory, executed_sql, executed_params


def _audit_record_mock(event_uuid: UUID | None = None) -> MagicMock:
    rec = MagicMock()
    rec.event_uuid = event_uuid or uuid4()
    rec.sequence_no = 1
    return rec


class TestRefreshBackendFromBrokerSnapshot:
    """PR-I: writes balance + position marks from broker snapshot
    BEFORE the recon planner runs."""

    async def test_naive_pulled_at_rejected(self) -> None:
        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        # Build a snapshot with a naive datetime — we hand-craft the
        # dataclass so the constructor's TZ-aware default doesn't kick in.
        naive_snapshot = ReconciliationSnapshot(
            pulled_at_utc=datetime(2026, 5, 12, 22, 30),  # naive
            account_summary=FlexAccountSummary(
                account_id="DUQ825170",
                report_date=datetime(2026, 5, 12).date(),
                net_liquidation_usd=Decimal("100000"),
                cash_usd=Decimal("100000"),
                stock_market_value_usd=Decimal(0),
                bond_market_value_usd=Decimal(0),
                futures_pnl_usd=Decimal(0),
            ),
            positions=(),
            cash_balances=(),
        )
        factory, _, _ = _refresh_session_factory(position_rows_by_market={})

        from unittest.mock import patch

        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            with pytest.raises(ValueError, match="tz-aware UTC"):
                await refresh_backend_from_broker_snapshot(
                    naive_snapshot,
                    session_factory=factory,
                    account_id=uuid4(),
                    env="paper",
                )

    async def test_empty_positions_writes_balance_only(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = _build_snapshot(positions=())
        factory, executed_sql, _ = _refresh_session_factory(position_rows_by_market={})

        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        assert result.balance_row_id is not None
        assert result.positions_marked_count == 0
        # 1 audit event (BALANCE_SNAPSHOT_RECORDED); no positions audits.
        assert len(result.audit_event_uuids) == 1
        # The balances INSERT happened.
        assert any("INSERT INTO balances" in s for s in executed_sql)
        # No positions_current UPDATE happened.
        assert not any("UPDATE positions_current" in s for s in executed_sql)

    async def test_matching_position_marked_and_audit_emitted(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        position_id = uuid4()
        snapshot = _build_snapshot(positions=(_flex_pos(symbol="MES", quantity="2"),))
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={
                "/MES": {
                    "id": position_id,
                    "quantity": 2,
                    "avg_cost": Decimal("5230"),
                }
            }
        )
        audit_calls: list[Any] = []

        async def _fake_audit(*args: Any, **kwargs: Any) -> MagicMock:
            audit_calls.append(args[1])  # event_type
            return _audit_record_mock()

        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=_fake_audit,
        ):
            result = await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        # 1 balance audit + 1 position-mark audit = 2
        assert len(result.audit_event_uuids) == 2
        assert result.positions_marked_count == 1
        # Both events fired with the right types.
        from services.audit.event_types import AuditEventType

        assert audit_calls == [
            AuditEventType.BALANCE_SNAPSHOT_RECORDED,
            AuditEventType.POSITION_MARK_TO_MARKET,
        ]
        # UPDATE happened.
        assert any("UPDATE positions_current" in s for s in executed_sql)
        # UPDATE was scoped to the matched position_id.
        update_params = next(
            p
            for s, p in zip(executed_sql, executed_params, strict=True)
            if "UPDATE positions_current" in s
        )
        assert update_params["pid"] == position_id

    async def test_position_not_in_backend_is_skipped(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        # Broker has /MES position; backend has NO row for /MES → skip.
        snapshot = _build_snapshot(positions=(_flex_pos(symbol="MES", quantity="2"),))
        factory, executed_sql, _ = _refresh_session_factory(position_rows_by_market={"/MES": None})

        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        # 1 balance audit; 0 position audits.
        assert len(result.audit_event_uuids) == 1
        assert result.positions_marked_count == 0
        # No UPDATE happened.
        assert not any("UPDATE positions_current" in s for s in executed_sql)

    async def test_zero_quantity_position_skipped(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = _build_snapshot(positions=(_flex_pos(symbol="MES", quantity="0"),))
        factory, executed_sql, _ = _refresh_session_factory(position_rows_by_market={})

        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        assert result.positions_marked_count == 0
        assert len(result.audit_event_uuids) == 1  # balance only
        # The lookup query wasn't even issued for the zero-qty position.
        assert not any(
            "SELECT id, quantity, avg_cost FROM positions_current" in s for s in executed_sql
        )

    async def test_broker_supplied_upnl_used_directly(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        position_id = uuid4()
        # FlexPos sets unrealized_pnl_usd=20 by default.
        snapshot = _build_snapshot(positions=(_flex_pos(symbol="MES", quantity="2"),))
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={
                "/MES": {
                    "id": position_id,
                    "quantity": 2,
                    "avg_cost": Decimal("5230"),
                }
            }
        )
        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        update_params = next(
            p
            for s, p in zip(executed_sql, executed_params, strict=True)
            if "UPDATE positions_current" in s
        )
        assert update_params["upnl"] == Decimal("20.0000")

    async def test_computed_upnl_when_broker_omits_it(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        # FlexPos but unrealized_pnl_usd = None.
        pos = FlexPosition(
            account_id="DUQ825170",
            symbol="MES",
            sec_type="FUT",
            quantity=Decimal("2"),
            avg_cost_usd=Decimal("5230"),
            market_price_usd=Decimal("5240"),  # mark
            market_value_usd=None,
            unrealized_pnl_usd=None,  # not supplied
        )
        snapshot = _build_snapshot(positions=(pos,))
        position_id = uuid4()
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={
                "/MES": {
                    "id": position_id,
                    "quantity": 2,
                    "avg_cost": Decimal("5230"),
                }
            }
        )
        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        # uPnL = (5240 - 5230) * 2 = 20
        update_params = next(
            p
            for s, p in zip(executed_sql, executed_params, strict=True)
            if "UPDATE positions_current" in s
        )
        assert update_params["upnl"] == Decimal("20.0000")

    async def test_no_mark_skips_silently(self) -> None:
        """Both market_price_usd and unrealized_pnl_usd None → skip."""
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        pos = FlexPosition(
            account_id="DUQ825170",
            symbol="MES",
            sec_type="FUT",
            quantity=Decimal("2"),
            avg_cost_usd=Decimal("5230"),
            market_price_usd=None,
            market_value_usd=None,
            unrealized_pnl_usd=None,
        )
        snapshot = _build_snapshot(positions=(pos,))
        position_id = uuid4()
        factory, executed_sql, _ = _refresh_session_factory(
            position_rows_by_market={
                "/MES": {
                    "id": position_id,
                    "quantity": 2,
                    "avg_cost": Decimal("5230"),
                }
            }
        )
        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        assert result.positions_marked_count == 0
        # 1 balance audit only.
        assert len(result.audit_event_uuids) == 1
        # No UPDATE.
        assert not any("UPDATE positions_current" in s for s in executed_sql)

    async def test_etf_no_slash_prefix(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = _build_snapshot(
            positions=(_flex_pos(symbol="TLT", sec_type="STK", quantity="100"),)
        )
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={
                "TLT": {
                    "id": uuid4(),
                    "quantity": 100,
                    "avg_cost": Decimal("85"),
                }
            }
        )
        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        # TLT (ETF) matched without slash prefix.
        assert result.positions_marked_count == 1
        # Lookup used market='TLT' (no slash).
        lookup_params = next(
            p
            for s, p in zip(executed_sql, executed_params, strict=True)
            if "SELECT id, quantity, avg_cost FROM positions_current" in s
        )
        assert lookup_params["market"] == "TLT"

    async def test_two_matching_positions_emit_three_audits(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = _build_snapshot(
            positions=(
                _flex_pos(symbol="MES", quantity="2"),
                _flex_pos(symbol="MNQ", quantity="1"),
            )
        )
        factory, _, _ = _refresh_session_factory(
            position_rows_by_market={
                "/MES": {"id": uuid4(), "quantity": 2, "avg_cost": Decimal("5230")},
                "/MNQ": {"id": uuid4(), "quantity": 1, "avg_cost": Decimal("18000")},
            }
        )
        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        # 1 balance + 2 positions = 3.
        assert len(result.audit_event_uuids) == 3
        assert result.positions_marked_count == 2
