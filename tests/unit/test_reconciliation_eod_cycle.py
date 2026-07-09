"""Unit tests for :mod:`services.reconciliation.eod_cycle`.

Pure-policy tests against the glue between
:func:`services.reconciliation.recon.plan_reconciliation_check`,
:func:`services.reconciliation.apply.apply_reconciliation_plan`, and
the Coinbase EOD fetcher (crypto-pivot C0 §3.5 — the FlexQuery fetch
path is deleted). No testcontainers — the DB-touching path
(``build_backend_view``) is exercised at integration-test time; this
file locks the orchestrator contract + the pure-policy view builders.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.audit.event_types import AuditEventType
from services.execution.types import BrokerPosition, FuturesBalanceSummary
from services.reconciliation.apply import AlertDispatchContext
from services.reconciliation.coinbase_fetcher import (
    CoinbaseReconFetchError,
    CoinbaseReconSnapshot,
    ReconPosition,
    recon_positions_from_broker,
)
from services.reconciliation.eod_cycle import (
    BALANCE_SOURCE_FROM_COINBASE,
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
from services.reconciliation.recon import BrokerSource, PriorBreak, ReconciliationMetric

_PULLED_AT = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)

# Test-fixture product ids (fakes; production discovers real ids at
# runtime per [A13]). These double as the ``positions_current.market``
# convention post-pivot: the venue product_id verbatim.
_BTC = "BTC-PERP-CDE"
_ETH = "ETH-PERP-CDE"


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


def _bpos(
    *,
    product_id: str = _BTC,
    contracts: str = "1",
    entry_vwap: str | None = "109000",
    mark_price: str | None = "110000",
    unrealized_pnl: str | None = "20",
) -> BrokerPosition:
    return BrokerPosition(
        product_id=product_id,
        contracts=Decimal(contracts),
        entry_vwap=Decimal(entry_vwap) if entry_vwap is not None else None,
        mark_price=Decimal(mark_price) if mark_price is not None else None,
        unrealized_pnl_usd=Decimal(unrealized_pnl) if unrealized_pnl is not None else None,
    )


def _bal_summary(
    *,
    total: str = "100000",
    unrealized: str | None = "0",
    available_margin: str | None = None,
    initial_margin: str | None = None,
) -> FuturesBalanceSummary:
    def _d(v: str | None) -> Decimal | None:
        return Decimal(v) if v is not None else None

    return FuturesBalanceSummary(
        total_usd_balance=Decimal(total),
        cbi_usd_balance=None,
        cfm_usd_balance=None,
        available_margin=_d(available_margin),
        initial_margin=_d(initial_margin),
        unrealized_pnl=_d(unrealized),
        daily_realized_pnl=None,
        liquidation_threshold=None,
        liquidation_buffer_amount=None,
        liquidation_buffer_percentage=None,
        snapshot_at_utc=_PULLED_AT,
    )


def _build_snapshot(
    *,
    positions: tuple[BrokerPosition, ...] = (),
    cash: str = "100000",
    nlv: str | None = None,
    summary: FuturesBalanceSummary | None = None,
    pulled_at_utc: datetime = _PULLED_AT,
) -> CoinbaseReconSnapshot:
    """Assemble a venue snapshot the way the fetcher would.

    ``positions`` are venue rows; the planner-ready ``ReconPosition``
    tuple is derived via the fetcher's own mapping function so the two
    can never drift in tests.
    """
    return CoinbaseReconSnapshot(
        positions=recon_positions_from_broker(positions),
        position_details=positions,
        balance_summary=summary if summary is not None else _bal_summary(total=cash),
        cash_usd=Decimal(cash),
        net_liquidation_usd=Decimal(nlv) if nlv is not None else Decimal(cash),
        fills=(),
        pulled_at_utc=pulled_at_utc,
    )


def _fake_fetcher(
    snapshot: CoinbaseReconSnapshot | None = None,
    *,
    error: CoinbaseReconFetchError | None = None,
) -> MagicMock:
    fetcher = MagicMock()
    if error is not None:
        fetcher.fetch_snapshot = AsyncMock(side_effect=error)
    else:
        fetcher.fetch_snapshot = AsyncMock(return_value=snapshot)
    return fetcher


def _config() -> EodCycleConfig:
    return EodCycleConfig(account_id=uuid4(), env="paper")


class TestEodCycleConfig:
    def test_shape_and_defaults(self) -> None:
        config = _config()
        assert config.phase_at_emit == 1

    def test_frozen(self) -> None:
        config = _config()
        with pytest.raises(AttributeError):
            config.env = "live-small"  # type: ignore[misc]

    def test_no_venue_credentials_on_config(self) -> None:
        # §3.5: venue credentials belong to the injected fetcher's
        # transport, not to the cycle config.
        field_names = set(EodCycleConfig.__dataclass_fields__)
        assert field_names == {"account_id", "env", "phase_at_emit"}


class TestBuildBrokerView:
    def test_empty_snapshot_returns_empty_view(self) -> None:
        view = build_broker_view(_build_snapshot(cash="0"))
        assert view.positions == {}
        assert view.cash_usd == Decimal("0")
        assert view.source == BrokerSource.COINBASE_EOD

    def test_market_is_product_id_verbatim(self) -> None:
        view = build_broker_view(
            _build_snapshot(positions=(_bpos(product_id=_BTC, contracts="2"),))
        )
        assert view.positions == {_BTC: Decimal("2")}

    def test_zero_quantity_dropped(self) -> None:
        view = build_broker_view(
            _build_snapshot(
                positions=(
                    _bpos(product_id=_BTC, contracts="0"),
                    _bpos(product_id=_ETH, contracts="1"),
                )
            )
        )
        assert view.positions == {_ETH: Decimal("1")}

    def test_same_market_sum_aggregates(self) -> None:
        view = build_broker_view(
            _build_snapshot(
                positions=(
                    _bpos(product_id=_BTC, contracts="2"),
                    _bpos(product_id=_BTC, contracts="3"),
                )
            )
        )
        assert view.positions == {_BTC: Decimal("5")}

    def test_short_position_negative_qty(self) -> None:
        view = build_broker_view(
            _build_snapshot(positions=(_bpos(product_id=_ETH, contracts="-4"),))
        )
        assert view.positions == {_ETH: Decimal("-4")}

    def test_cash_from_snapshot(self) -> None:
        view = build_broker_view(_build_snapshot(cash="12345.6789"))
        assert view.cash_usd == Decimal("12345.6789")

    def test_positions_override_seam(self) -> None:
        # The seam preserved for the Phase C1 intraday probe: supplied
        # ReconPositions replace the snapshot's, and the caller stamps
        # the true source.
        snapshot = _build_snapshot(positions=(_bpos(product_id=_BTC, contracts="2"),))
        override = (
            ReconPosition(market=_ETH, quantity=Decimal("7")),
            ReconPosition(market=_BTC, quantity=Decimal("0")),  # re-guarded
        )
        view = build_broker_view(
            snapshot,
            positions_override=override,
            source=BrokerSource.COINBASE_EOD,
        )
        assert view.positions == {_ETH: Decimal("7")}
        # Cash still comes from the snapshot regardless of the override.
        assert view.cash_usd == snapshot.cash_usd


def _stub_session_factory(
    *,
    positions: list[dict[str, Any]],
    balance: dict[str, Any] | None,
    prior_breaks: list[dict[str, Any]] | None = None,
) -> Any:
    """Build a fake session_factory that returns the supplied snapshot rows.

    ``prior_breaks`` defaults to ``[]`` — the empty list models the
    day-1 boot path (no prior cycles have populated
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
    async def test_fetch_failure_returns_none_no_db_writes(self) -> None:
        fetcher = _fake_fetcher(
            error=CoinbaseReconFetchError(operation="list_positions", detail="venue 503")
        )
        result = await run_eod_cycle(
            config=_config(),
            session_factory=_stub_session_factory(positions=[], balance=None),
            fetcher=fetcher,
        )
        assert result is None

    async def test_happy_path_with_no_breaks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Venue + backend agree exactly on positions + cash.
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 1}],
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
            config=_config(), session_factory=factory, fetcher=_fake_fetcher(snap)
        )
        assert result is not None
        # No breaks because backend and broker match exactly.
        assert len(captured["plan"].breaks_detected) == 0
        # check_passed audit event still emitted (per planner contract).
        assert len(captured["plan"].audit_events) == 1

    async def test_position_break_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Broker has 1 contract; backend has 2. Position tolerance is
        # exact-match per recon planner so this is an actionable break.
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 2}],
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

        await run_eod_cycle(config=_config(), session_factory=factory, fetcher=_fake_fetcher(snap))
        assert len(captured["plan"].breaks_detected) == 1
        b = captured["plan"].breaks_detected[0]
        assert b.market == _BTC
        assert b.expected == Decimal("2")
        assert b.actual == Decimal("1")
        assert b.delta == Decimal("1")  # |expected - actual|

    async def test_no_balance_row_treats_cash_as_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snap = _build_snapshot(cash="5000")
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

        await run_eod_cycle(config=_config(), session_factory=factory, fetcher=_fake_fetcher(snap))
        # Backend cash=0, broker cash=5000 → cash_usd break expected
        # (delta > tolerance since equity_baseline=0 means bps tolerance=0).
        plan = captured["plan"]
        assert any(b.metric.value == "cash_usd" for b in plan.breaks_detected)


class TestRunEodCycleBrokerMissingPositionsWarning:
    """Resilience signal (venue-neutral successor of the 2026-05-27
    missing-FUT warning): when backend has open positions but the venue
    returned ZERO position rows, emit a structured warning naming the
    suspected root causes so triage doesn't require reading fetcher
    source. Non-blocking — the planner still runs + breaks still land.
    """

    async def test_warning_fires_when_backend_has_positions_but_broker_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        # Venue snapshot: ZERO positions.
        snap = _build_snapshot(cash="100000")
        # Backend: one open BTC perp position.
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 1}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        with structlog.testing.capture_logs() as captured:
            await run_eod_cycle(
                config=_config(), session_factory=factory, fetcher=_fake_fetcher(snap)
            )

        events = [c.get("event") for c in captured]
        assert "reconciliation_eod_cycle_broker_view_missing_positions" in events
        warning = next(
            c
            for c in captured
            if c.get("event") == "reconciliation_eod_cycle_broker_view_missing_positions"
        )
        assert warning["backend_markets"] == [_BTC]
        assert warning["backend_position_count"] == 1
        assert warning["broker_position_count"] == 0
        assert warning["log_level"] == "warning"
        # The hint names suspected root causes for fast triage.
        assert "CDP key" in warning["hint"]
        assert "list_positions" in warning["hint"]

    async def test_warning_silent_when_broker_has_positions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Happy path: venue has position rows, so the resilience signal
        # stays silent. We must not log on every healthy cycle.
        import structlog

        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 1}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        with structlog.testing.capture_logs() as captured:
            await run_eod_cycle(
                config=_config(), session_factory=factory, fetcher=_fake_fetcher(snap)
            )

        events = [c.get("event") for c in captured]
        assert "reconciliation_eod_cycle_broker_view_missing_positions" not in events

    async def test_warning_silent_when_backend_flat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Flat backend book + empty venue book: absence is the correct
        # state, not a fetch degradation — no warning.
        import structlog

        snap = _build_snapshot(cash="5000")
        factory = _stub_session_factory(
            positions=[],
            balance={"cash_usd": Decimal("5000"), "net_liquidation": Decimal("5000")},
        )

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        with structlog.testing.capture_logs() as captured:
            await run_eod_cycle(
                config=_config(), session_factory=factory, fetcher=_fake_fetcher(snap)
            )

        events = [c.get("event") for c in captured]
        assert "reconciliation_eod_cycle_broker_view_missing_positions" not in events


class TestMakeCycleCallback:
    async def test_callback_invokes_run_eod_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config()
        factory = _stub_session_factory(positions=[], balance=None)
        fetcher = _fake_fetcher(_build_snapshot())
        invoked = AsyncMock(return_value=None)
        monkeypatch.setattr("services.reconciliation.eod_cycle.run_eod_cycle", invoked)
        cb = make_cycle_callback(config=config, session_factory=factory, fetcher=fetcher)
        await cb(datetime(2026, 7, 10, tzinfo=UTC).date())
        invoked.assert_awaited_once()
        kwargs = invoked.await_args.kwargs  # type: ignore[union-attr]
        assert kwargs["config"] is config
        assert kwargs["session_factory"] is factory
        assert kwargs["fetcher"] is fetcher

    async def test_callback_threads_alert_dispatch_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # make_cycle_callback accepts an alert_dispatch_hook and threads
        # it through to run_eod_cycle so the api lifespan glue can inject
        # a Discord-fan-out hook at scheduler construction time.
        config = _config()
        factory = _stub_session_factory(positions=[], balance=None)
        invoked = AsyncMock(return_value=None)
        monkeypatch.setattr("services.reconciliation.eod_cycle.run_eod_cycle", invoked)

        async def my_hook(ctx: AlertDispatchContext) -> None:
            return None

        cb = make_cycle_callback(
            config=config,
            session_factory=factory,
            fetcher=_fake_fetcher(_build_snapshot()),
            alert_dispatch_hook=my_hook,
        )
        await cb(datetime(2026, 7, 10, tzinfo=UTC).date())
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
        config = _config()
        # 1-contract position divergence on BTC → 1 actionable break.
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 2}],
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
            fetcher=_fake_fetcher(snap),
            alert_dispatch_hook=hook,
        )

        # Exactly one hook invocation for the actionable break.
        assert len(hook_calls) == 1
        ctx = hook_calls[0]
        # Hook received the descriptor for the break.
        assert ctx.descriptor.severity == "P2"
        assert ctx.descriptor.category == "reconciliation_break"
        assert ctx.descriptor.payload["market"] == _BTC
        assert ctx.descriptor.payload["delta"] == "1"
        # Hook received the context fields populated.
        assert ctx.account_id == config.account_id
        assert ctx.env == config.env
        # Plan reached apply with the hook in kwargs.
        assert captured_plan["kwargs"]["alert_dispatch_hook"] is hook

    async def test_no_breaks_no_hook_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config()
        # Venue + backend agree exactly on positions + cash.
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 1}],
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
            fetcher=_fake_fetcher(snap),
            alert_dispatch_hook=hook,
        )
        # No breaks → no alerts → no hook calls.
        assert hook_calls == []

    async def test_multiple_actionable_breaks_fire_hook_per_break(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Three actionable breaks → three hook invocations, in order.
        config = _config()
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        # Backend says different positions on BTC + ETH and cash mismatch.
        factory = _stub_session_factory(
            positions=[
                {"market": _BTC, "qty": 3},
                {"market": _ETH, "qty": 1},
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
            fetcher=_fake_fetcher(snap),
            alert_dispatch_hook=hook,
        )
        # Three breaks (BTC, ETH, cash) → three hook invocations.
        assert len(hook_calls) == 3
        markets = [ctx.descriptor.payload["market"] for ctx in hook_calls]
        # Position breaks sorted by market; cash break has market=None.
        assert markets == [_BTC, _ETH, None]

    async def test_hook_optional_when_no_hook_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-wiring boot: no hook supplied → cycle still completes
        # (audit + reconciliation_breaks rows still land via apply).
        config = _config()
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 2}],
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
            fetcher=_fake_fetcher(snap),
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

    * Empty table → ``()`` (day-1 boot).
    * Populated table → mapped to ``PriorBreak(metric, market, delta)``.
    * Unknown metric → skipped + warning emitted (defensive against a
      future metric value landing before the planner enum is updated).
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
            prior_breaks=[{"metric": "position_qty", "market": _BTC, "delta": Decimal("1")}],
        )
        prior = await fetch_prior_breaks_within_grace_window(factory, account_id=uuid4())
        assert prior == (
            PriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market=_BTC,
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
        # A future metric (e.g., 'net_liquidation') landing in the
        # schema before the planner enum is updated should be silently
        # dropped — never crash the cycle.
        factory = _stub_session_factory(
            positions=[],
            balance=None,
            prior_breaks=[
                {"metric": "net_liquidation", "market": None, "delta": Decimal("100.00")},
                {"metric": "position_qty", "market": _BTC, "delta": Decimal("1")},
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
        # Crypto trades 24/7 so there is no weekend gap to bridge.
        assert DEFAULT_PRIOR_BREAKS_WINDOW_HOURS == 36

    async def test_run_eod_cycle_threads_prior_breaks_to_planner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: run_eod_cycle calls the helper + threads the result
        # into plan_reconciliation_check. Verifies the prior-break is
        # classified as grace continuation (no alert fires).
        config = _config()
        # Venue + backend disagree on BTC by 1 contract — same delta
        # as the prior break we'll seed below.
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 2}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
            # Prior break with same (metric, market, delta) as today's
            # break — should classify as grace, drop the alert.
            prior_breaks=[{"metric": "position_qty", "market": _BTC, "delta": Decimal("1")}],
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

        await run_eod_cycle(config=config, session_factory=factory, fetcher=_fake_fetcher(snap))

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
        config = _config()
        snap = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="1"),),
            cash="100000",
        )
        factory = _stub_session_factory(
            positions=[{"market": _BTC, "qty": 3}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
            # Prior delta=1, today delta=2 — different → not grace.
            prior_breaks=[{"metric": "position_qty", "market": _BTC, "delta": Decimal("1")}],
        )

        captured: dict[str, Any] = {}

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            return MagicMock(alerts_dispatched_count=0, kill_switch_invoked=False)

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        await run_eod_cycle(config=config, session_factory=factory, fetcher=_fake_fetcher(snap))

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

    def test_balance_source_matches_api_repo_filter(self) -> None:
        # The /system tile's freshness signal filters balances rows on
        # the same source string the recon cycle writes — keep in sync.
        from services.api.repos.phase1 import _RECON_BALANCE_SOURCE

        assert BALANCE_SOURCE_FROM_COINBASE == _RECON_BALANCE_SOURCE == "coinbase_eod"

    def test_balance_source_matches_broker_source_enum(self) -> None:
        assert BALANCE_SOURCE_FROM_COINBASE == BrokerSource.COINBASE_EOD.value


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
    """PR-I: writes balance + position marks from the venue snapshot
    BEFORE the recon planner runs."""

    async def test_naive_pulled_at_rejected(self) -> None:
        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        naive_snapshot = CoinbaseReconSnapshot(
            positions=(),
            position_details=(),
            balance_summary=_bal_summary(),
            cash_usd=Decimal("100000"),
            net_liquidation_usd=Decimal("100000"),
            fills=(),
            pulled_at_utc=datetime(2026, 7, 10, 0, 15),  # naive
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

    async def test_balance_insert_params_coinbase_semantics(self) -> None:
        # source='coinbase_eod'; NLV/cash from the snapshot; excess from
        # available_margin; used_margin_pct = initial_margin / NLV.
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = CoinbaseReconSnapshot(
            positions=(),
            position_details=(),
            balance_summary=_bal_summary(
                total="100000",
                unrealized="150",
                available_margin="35000",
                initial_margin="5000",
            ),
            cash_usd=Decimal("100000"),
            net_liquidation_usd=Decimal("100150"),
            fills=(),
            pulled_at_utc=_PULLED_AT,
        )
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={}
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
        insert_params = next(
            p
            for s, p in zip(executed_sql, executed_params, strict=True)
            if "INSERT INTO balances" in s
        )
        assert insert_params["source"] == BALANCE_SOURCE_FROM_COINBASE
        assert insert_params["nlv"] == Decimal("100150")
        assert insert_params["cash"] == Decimal("100000")
        assert insert_params["excess"] == Decimal("35000")
        # 5000 / 100150 quantized to NUMERIC(10, 8).
        assert insert_params["margin"] == Decimal("0.04992511")

    async def test_balance_insert_margin_fallbacks(self) -> None:
        # Venue omits available_margin + initial_margin → excess falls
        # back to cash; used_margin_pct falls back to 0.
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = _build_snapshot(cash="50000")
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={}
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
        insert_params = next(
            p
            for s, p in zip(executed_sql, executed_params, strict=True)
            if "INSERT INTO balances" in s
        )
        assert insert_params["excess"] == Decimal("50000")
        assert insert_params["margin"] == Decimal(0)

    async def test_balance_audit_payload_carries_raw_venue_fields(self) -> None:
        # The audit payload stamps the raw venue money fields as strings
        # ([A05]) so venue semantics surprises are forensically visible.
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = CoinbaseReconSnapshot(
            positions=(),
            position_details=(),
            balance_summary=_bal_summary(
                total="100000",
                unrealized="150",
                available_margin=None,
                initial_margin=None,
            ),
            cash_usd=Decimal("100000"),
            net_liquidation_usd=Decimal("100150"),
            fills=(),
            pulled_at_utc=_PULLED_AT,
        )
        factory, _, _ = _refresh_session_factory(position_rows_by_market={})
        append = AsyncMock(side_effect=lambda *a, **k: _audit_record_mock())
        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=append,
        ):
            await refresh_backend_from_broker_snapshot(
                snapshot,
                session_factory=factory,
                account_id=uuid4(),
                env="paper",
            )
        payload = append.await_args_list[0].args[2]
        assert payload["source"] == BALANCE_SOURCE_FROM_COINBASE
        assert payload["broker_cash_usd"] == "100000"
        assert payload["broker_net_liquidation_usd"] == "100150"
        assert payload["broker_total_usd_balance"] == "100000"
        assert payload["broker_unrealized_pnl"] == "150"
        assert payload["broker_available_margin"] is None
        assert payload["fill_count_in_snapshot"] == 0

    async def test_matching_position_marked_and_audit_emitted(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        position_id = uuid4()
        snapshot = _build_snapshot(positions=(_bpos(product_id=_BTC, contracts="2"),))
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={
                _BTC: {
                    "id": position_id,
                    "quantity": 2,
                    "avg_cost": Decimal("109000"),
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

        # Broker has a BTC position; backend has NO row for it → skip.
        snapshot = _build_snapshot(positions=(_bpos(product_id=_BTC, contracts="2"),))
        factory, executed_sql, _ = _refresh_session_factory(position_rows_by_market={_BTC: None})

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

        snapshot = _build_snapshot(positions=(_bpos(product_id=_BTC, contracts="0"),))
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

    async def test_broker_supplied_upnl_used_and_quantized(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        position_id = uuid4()
        snapshot = _build_snapshot(
            positions=(_bpos(product_id=_BTC, contracts="2", unrealized_pnl="20.123456"),)
        )
        factory, executed_sql, executed_params = _refresh_session_factory(
            position_rows_by_market={
                _BTC: {
                    "id": position_id,
                    "quantity": 2,
                    "avg_cost": Decimal("109000"),
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
        # Quantized half-even to NUMERIC(20, 4).
        assert update_params["upnl"] == Decimal("20.1235")

    async def test_missing_upnl_skips_mark_with_warning(self) -> None:
        """Venue omits unrealized_pnl → the mark is SKIPPED (the
        FlexQuery-era price-minus-cost fallback is deliberately gone —
        it is wrong for futures without the contract multiplier)."""
        from unittest.mock import patch

        import structlog

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = _build_snapshot(
            positions=(
                _bpos(product_id=_BTC, contracts="2", mark_price="110000", unrealized_pnl=None),
            )
        )
        position_id = uuid4()
        factory, executed_sql, _ = _refresh_session_factory(
            position_rows_by_market={
                _BTC: {
                    "id": position_id,
                    "quantity": 2,
                    "avg_cost": Decimal("109000"),
                }
            }
        )
        with patch(
            "services.reconciliation.eod_cycle.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            with structlog.testing.capture_logs() as captured:
                result = await refresh_backend_from_broker_snapshot(
                    snapshot,
                    session_factory=factory,
                    account_id=uuid4(),
                    env="paper",
                )
        assert result.positions_marked_count == 0
        # 1 balance audit only.
        assert len(result.audit_event_uuids) == 1
        # No UPDATE — even though a mark price exists, we refuse the
        # multiplier-less computation.
        assert not any("UPDATE positions_current" in s for s in executed_sql)
        events = [c.get("event") for c in captured]
        assert "reconciliation_refresh_no_broker_upnl" in events

    async def test_two_matching_positions_emit_three_audits(self) -> None:
        from unittest.mock import patch

        refresh_backend_from_broker_snapshot = _real_refresh  # captured before autouse monkeypatch

        snapshot = _build_snapshot(
            positions=(
                _bpos(product_id=_BTC, contracts="2"),
                _bpos(product_id=_ETH, contracts="-1", unrealized_pnl="-3.5"),
            )
        )
        factory, _, _ = _refresh_session_factory(
            position_rows_by_market={
                _BTC: {"id": uuid4(), "quantity": 2, "avg_cost": Decimal("109000")},
                _ETH: {"id": uuid4(), "quantity": -1, "avg_cost": Decimal("3900")},
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


# ---------------------------------------------------------------------------
# PR-K wiring (2026-05-16) — attribution rollup at recon end
# ---------------------------------------------------------------------------


class TestFetchClosedTradesForSessionDate:
    """The SELECT helper for PR-K's daily rollup.

    Today (pre-exit-fill-path) every test returns empty — no trades have
    state='closed' because services/risk/fill_processor.py raises
    UnsupportedFillScenarioError on exit fills. The empty-result path
    is the most important contract right now.
    """

    @pytest.mark.asyncio
    async def test_empty_when_no_closed_trades(self) -> None:
        from datetime import date

        from services.reconciliation.eod_cycle import fetch_closed_trades_for_session_date

        # Fake session.execute returns 0 rows.
        @asynccontextmanager
        async def _session_cm() -> Any:
            sess = MagicMock()
            res = MagicMock()
            res.fetchall = MagicMock(return_value=[])
            sess.execute = AsyncMock(return_value=res)
            yield sess

        factory = MagicMock(side_effect=lambda: _session_cm())
        result = await fetch_closed_trades_for_session_date(
            factory,
            account_id=uuid4(),
            env="paper",
            session_date_et=date(2026, 7, 9),
        )
        assert result == ()

    @pytest.mark.asyncio
    async def test_populated_trades_map_to_dataclass(self) -> None:
        from datetime import date

        from services.reconciliation.eod_cycle import fetch_closed_trades_for_session_date

        trade_id = uuid4()
        signal_id = uuid4()
        opened_at = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)
        closed_at = datetime(2026, 7, 9, 20, 30, tzinfo=UTC)
        row = MagicMock()
        row.trade_id = trade_id
        row.entry_signal_id = signal_id
        row.direction = "long"
        row.total_quantity = 1
        row.avg_entry_price = Decimal("85.50")
        row.avg_exit_price = Decimal("86.25")
        row.realized_pnl_usd = Decimal("0.75")
        row.realized_commission_usd = Decimal("0.05")
        row.opened_at_utc = opened_at
        row.closed_at_utc = closed_at
        row.expected_entry_price = Decimal("85.40")
        row.expected_slippage_bps = Decimal("1.2")
        row.expected_at_utc = opened_at

        @asynccontextmanager
        async def _session_cm() -> Any:
            sess = MagicMock()
            res = MagicMock()
            res.fetchall = MagicMock(return_value=[row])
            sess.execute = AsyncMock(return_value=res)
            yield sess

        factory = MagicMock(side_effect=lambda: _session_cm())
        result = await fetch_closed_trades_for_session_date(
            factory,
            account_id=uuid4(),
            env="paper",
            session_date_et=date(2026, 7, 9),
        )
        assert len(result) == 1
        trade = result[0]
        assert trade.trade_id == trade_id
        assert trade.entry_signal_id == signal_id
        assert trade.direction == "long"
        assert trade.total_quantity == 1
        assert trade.avg_entry_price == Decimal("85.50")
        assert trade.realized_pnl_usd == Decimal("0.75")
        assert trade.expected_entry_price == Decimal("85.40")

    @pytest.mark.asyncio
    async def test_incomplete_trade_skipped_silently(self) -> None:
        """Defensive: a trade in state='closed' with null avg_exit_price
        is a data-quality concern (shouldn't be possible per PR-G+
        contract); skip rather than crash."""
        from datetime import date

        from services.reconciliation.eod_cycle import fetch_closed_trades_for_session_date

        row = MagicMock()
        row.trade_id = uuid4()
        row.entry_signal_id = uuid4()
        row.direction = "long"
        row.total_quantity = 1
        row.avg_entry_price = Decimal("85.50")
        row.avg_exit_price = None  # incomplete
        row.realized_pnl_usd = Decimal("0.50")
        row.realized_commission_usd = Decimal("0.05")
        row.opened_at_utc = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)
        row.closed_at_utc = datetime(2026, 7, 9, 20, 30, tzinfo=UTC)
        row.expected_entry_price = Decimal("85.40")
        row.expected_slippage_bps = Decimal("1.2")
        row.expected_at_utc = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)

        @asynccontextmanager
        async def _session_cm() -> Any:
            sess = MagicMock()
            res = MagicMock()
            res.fetchall = MagicMock(return_value=[row])
            sess.execute = AsyncMock(return_value=res)
            yield sess

        factory = MagicMock(side_effect=lambda: _session_cm())
        result = await fetch_closed_trades_for_session_date(
            factory,
            account_id=uuid4(),
            env="paper",
            session_date_et=date(2026, 7, 9),
        )
        assert result == ()  # skipped


class TestEmitDailyAttributionRollup:
    """The PR-K helper that fires from inside run_eod_cycle."""

    @pytest.mark.asyncio
    async def test_empty_trades_writes_audit_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Today's reality: no closed trades → no attribution rows
        INSERTed, but the audit ROLL-UP event still fires (Today page
        breadcrumb)."""
        from services.reconciliation import eod_cycle as mod

        # Stub the SELECT to return empty
        async def _stub_fetch(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
            return ()

        monkeypatch.setattr(mod, "fetch_closed_trades_for_session_date", _stub_fetch)

        # Stub append_audit_event
        rec = MagicMock()
        rec.event_uuid = uuid4()
        rec.sequence_no = 30
        append = AsyncMock(return_value=rec)
        import services.risk.attribution as attr_mod

        monkeypatch.setattr(attr_mod, "append_audit_event", append)

        @asynccontextmanager
        async def _session_cm() -> Any:
            sess = MagicMock()
            sess.execute = AsyncMock()
            sess.begin = lambda: _asyncnull_cm()
            yield sess

        @asynccontextmanager
        async def _asyncnull_cm() -> Any:
            yield None

        factory = MagicMock(side_effect=lambda: _session_cm())

        count = await mod._emit_daily_attribution_rollup(
            session_factory=factory,
            account_id=uuid4(),
            env="paper",
            phase_at_emit=1,
            snapshot_pulled_at_utc=datetime(2026, 7, 10, 0, 15, tzinfo=UTC),
        )
        # Zero rows inserted (no closed trades)
        assert count == 0
        # Audit event fired exactly once (the rollup breadcrumb)
        append.assert_awaited_once()

        assert append.await_args.args[1] == AuditEventType.ATTRIBUTION_ROLLUP_RECORDED
        # Payload carries the zero-aggregate
        payload = append.await_args.args[2]
        assert payload["trade_count"] == 0
        assert payload["long_count"] == 0
        assert payload["short_count"] == 0


class TestRunEodCycleAttributionIntegration:
    """Verify the attribution rollup is invoked at the end of run_eod_cycle."""

    @staticmethod
    def _stub_recon_stages(monkeypatch: pytest.MonkeyPatch) -> Any:
        """Stub backend view / prior breaks / apply; return apply_result."""
        from services.reconciliation import eod_cycle as mod
        from services.reconciliation.apply import ReconciliationApplyResult
        from services.reconciliation.recon import BackendView

        monkeypatch.setattr(
            mod,
            "build_backend_view",
            AsyncMock(
                return_value=BackendView(
                    positions={}, cash_usd=Decimal("0"), equity_baseline=Decimal("0")
                )
            ),
        )
        monkeypatch.setattr(
            mod,
            "fetch_prior_breaks_within_grace_window",
            AsyncMock(return_value=()),
        )
        apply_result = ReconciliationApplyResult(
            audit_event_uuids=(),
            inserted_break_ids=(),
            resolved_break_count=0,
            kill_switch_invoked=False,
            alerts_dispatched_count=0,
        )
        monkeypatch.setattr(
            mod,
            "apply_reconciliation_plan",
            AsyncMock(return_value=apply_result),
        )
        return apply_result

    @pytest.mark.asyncio
    async def test_attribution_helper_called_from_run_eod_cycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wiring must call _emit_daily_attribution_rollup from
        inside run_eod_cycle after apply_reconciliation_plan succeeds."""
        from services.reconciliation import eod_cycle as mod

        snap = _build_snapshot(cash="0")
        self._stub_recon_stages(monkeypatch)

        # Spy on the attribution helper
        emit_spy = AsyncMock(return_value=0)
        monkeypatch.setattr(mod, "_emit_daily_attribution_rollup", emit_spy)

        # Fake session factory
        @asynccontextmanager
        async def _session_cm() -> Any:
            yield MagicMock()

        session_factory = MagicMock(side_effect=lambda: _session_cm())

        await mod.run_eod_cycle(
            config=_config(),
            session_factory=session_factory,
            fetcher=_fake_fetcher(snap),
        )

        # Attribution helper was called
        emit_spy.assert_awaited_once()
        kwargs = emit_spy.await_args.kwargs
        assert kwargs["env"] == "paper"
        # snapshot_pulled_at_utc was forwarded
        assert kwargs["snapshot_pulled_at_utc"] == snap.pulled_at_utc

    @pytest.mark.asyncio
    async def test_attribution_failure_does_not_fail_cycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If _emit_daily_attribution_rollup raises, run_eod_cycle still
        returns the recon apply result. PR-K is best-effort."""
        from services.reconciliation import eod_cycle as mod

        snap = _build_snapshot(cash="0")
        apply_result = self._stub_recon_stages(monkeypatch)

        # Attribution helper raises
        monkeypatch.setattr(
            mod,
            "_emit_daily_attribution_rollup",
            AsyncMock(side_effect=RuntimeError("audit chain down")),
        )

        @asynccontextmanager
        async def _session_cm() -> Any:
            yield MagicMock()

        session_factory = MagicMock(side_effect=lambda: _session_cm())

        # Cycle still returns successfully (apply_result)
        result = await mod.run_eod_cycle(
            config=_config(),
            session_factory=session_factory,
            fetcher=_fake_fetcher(snap),
        )
        assert result is apply_result
