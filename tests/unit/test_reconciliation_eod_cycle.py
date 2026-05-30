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

from services.audit.event_types import AuditEventType
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
from services.reconciliation.ibkr_intraday import (
    ReconPosition,
    ReconPositionsFetchError,
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
    account_summary_cash_usd: str = "0",
) -> ReconciliationSnapshot:
    summary = FlexAccountSummary(
        account_id="DUQ825170",
        report_date=datetime(2026, 5, 12).date(),
        net_liquidation_usd=Decimal(nav),
        cash_usd=Decimal(account_summary_cash_usd),
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
    underlying_symbol: str | None = None,
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
        underlying_symbol=underlying_symbol,
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


class TestBuildBrokerViewFuturesNormalization:
    """Tests for the 2026-05-27 fix: prefer ``underlying_symbol`` over
    ``symbol`` for FUT positions so the broker view's market key matches
    the backend's root-ticker convention.

    Failure mode this locks down: pre-fix, a FlexQuery template emitting
    ``symbol="M2KM6"`` (contract-month form, June 2026 micro Russell
    2000) produced a broker-view market of ``"/M2KM6"``, which never
    matched backend's ``"/M2K"`` → false-positive ``position_qty`` break
    every EOD cycle. The 2026-05-27 22:30 UTC recon fired this exact case.
    """

    def test_fut_with_underlying_symbol_uses_underlying(self) -> None:
        # The production-shape XML: symbol=M2KM6 (contract month),
        # underlyingSymbol=M2K (root ticker). The broker view's market
        # MUST normalize to /M2K so it matches backend's positions_current.
        snap = _build_snapshot(
            positions=(
                _flex_pos(
                    symbol="M2KM6",
                    sec_type="FUT",
                    quantity="1",
                    underlying_symbol="M2K",
                ),
            )
        )
        view = build_broker_view(snap)
        assert view.positions == {"/M2K": Decimal("1")}

    def test_fut_without_underlying_symbol_falls_back_to_symbol(self) -> None:
        # Older templates (and the sample XML in our test corpus) emit
        # symbol=MES (root) with no underlyingSymbol attribute → parser
        # defaults underlying_symbol to None → view builder falls back to
        # symbol. Backwards-compat contract.
        snap = _build_snapshot(
            positions=(
                _flex_pos(symbol="MES", sec_type="FUT", quantity="1", underlying_symbol=None),
            )
        )
        view = build_broker_view(snap)
        assert view.positions == {"/MES": Decimal("1")}

    def test_fut_with_empty_underlying_symbol_falls_back_to_symbol(self) -> None:
        # The parser converts empty-string underlyingSymbol to None, so
        # this case shouldn't reach the view builder in practice. But
        # the view builder is defensive: an empty/falsy underlying_symbol
        # also falls back to ``symbol``.
        snap = _build_snapshot(
            positions=(_flex_pos(symbol="MES", sec_type="FUT", quantity="1", underlying_symbol=""),)
        )
        view = build_broker_view(snap)
        assert view.positions == {"/MES": Decimal("1")}

    def test_stk_ignores_underlying_symbol(self) -> None:
        # STK rows (ETFs / equities) have no derivative root; their
        # ``symbol`` IS the canonical identifier. Even if the FlexQuery
        # template somehow populates underlyingSymbol on a STK row, the
        # view builder must NOT prepend the slash + must NOT use
        # underlyingSymbol (that would silently break ETF reconciliation).
        snap = _build_snapshot(
            positions=(
                _flex_pos(
                    symbol="TLT",
                    sec_type="STK",
                    quantity="100",
                    underlying_symbol="NOT_USED",
                ),
            )
        )
        view = build_broker_view(snap)
        assert view.positions == {"TLT": Decimal("100")}

    def test_fut_contract_month_rows_aggregate_on_root(self) -> None:
        # A roll-window snapshot could carry both M2KM6 + M2KU6 (June +
        # September 2026 contracts) — both underlyingSymbol=M2K. After
        # normalization, both rows aggregate onto the single ``/M2K``
        # market. Mirrors the test_same_market_sum_aggregates contract
        # but exercises the normalization path.
        snap = _build_snapshot(
            positions=(
                _flex_pos(
                    symbol="M2KM6",
                    sec_type="FUT",
                    quantity="1",
                    underlying_symbol="M2K",
                ),
                _flex_pos(
                    symbol="M2KU6",
                    sec_type="FUT",
                    quantity="2",
                    underlying_symbol="M2K",
                ),
            )
        )
        view = build_broker_view(snap)
        assert view.positions == {"/M2K": Decimal("3")}


class TestBuildBrokerViewCashFallback:
    """Tests for the 2026-05-16 fix: fall back to account_summary.cash_usd
    when cash_balances is empty.

    Real-world FlexQuery templates often have the AccountInformation
    section (populates account_summary.cash_usd) enabled but NOT the
    Cash Report section (populates cash_balances). Without the
    fallback, the recon planner sees backend_cash from PR-I (=
    account_summary.cash_usd) vs broker_cash from build_broker_view
    (= sum of cash_balances = 0) and emits a false-positive break
    every cycle.
    """

    def test_empty_cash_balances_falls_back_to_account_summary(self) -> None:
        import structlog

        snap = _build_snapshot(
            cash_balances=(),
            account_summary_cash_usd="19997.51",
        )
        with structlog.testing.capture_logs() as captured:
            view = build_broker_view(snap)
        assert view.cash_usd == Decimal("19997.51")
        # Fallback emits a structured log so the operator can see why we
        # diverged from the Cash Report path.
        events = [c.get("event") for c in captured]
        assert "recon_broker_view_cash_fallback_to_account_summary" in events
        fallback = next(
            c
            for c in captured
            if c.get("event") == "recon_broker_view_cash_fallback_to_account_summary"
        )
        assert fallback["cash_balances_count"] == 0
        assert fallback["account_summary_cash_usd"] == "19997.51"

    def test_empty_cash_balances_with_zero_account_summary_still_zero(self) -> None:
        # When BOTH sources are 0 (truly no cash), fallback returns 0;
        # no false-positive triggered.
        snap = _build_snapshot(cash_balances=(), account_summary_cash_usd="0")
        view = build_broker_view(snap)
        assert view.cash_usd == Decimal("0")

    def test_non_usd_only_balances_falls_back(self) -> None:
        # A template with Cash Report enabled but the account holds
        # only EUR cash — no USD row. We still fall back to the
        # account_summary's USD value (which may be the USD-converted
        # NAV from the AccountInformation section).
        snap = _build_snapshot(
            cash_balances=(FlexCashBalance(currency="EUR", balance=Decimal("500")),),
            account_summary_cash_usd="1000.00",
        )
        view = build_broker_view(snap)
        assert view.cash_usd == Decimal("1000.00")  # not the EUR balance

    def test_populated_usd_cash_balances_does_not_fall_back(self) -> None:
        # When cash_balances HAS a USD row, that's the source — the
        # account_summary value is ignored (the per-currency Cash Report
        # is more granular + the spec-canonical source).
        snap = _build_snapshot(
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("5000")),),
            account_summary_cash_usd="9999",  # different value; must be ignored
        )
        view = build_broker_view(snap)
        assert view.cash_usd == Decimal("5000")

    def test_zero_usd_cash_balance_is_authoritative_not_fallback(self) -> None:
        # If the template has Cash Report enabled AND the account
        # actually has $0 USD (recorded as a USD row with balance=0),
        # we trust that — don't fall back. The fallback fires ONLY when
        # there's no USD row at all.
        snap = _build_snapshot(
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("0")),),
            account_summary_cash_usd="9999",  # ignored — USD row is authoritative
        )
        view = build_broker_view(snap)
        assert view.cash_usd == Decimal("0")


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


class TestRunEodCycleBrokerMissingFuturesWarning:
    """Tests for the 2026-05-27 resilience signal: when backend has open
    FUT positions but the broker view returned ZERO FUT rows, emit a
    structured warning naming the suspected root cause (FlexQuery template
    missing OpenPositions FUT section).

    Without this signal, the recon planner emits a position_qty break per
    backend FUT market every cycle + the operator sees "false break" with
    no pointer at the underlying template-config issue. The 2026-05-27
    22:30 UTC false-positive landed in audit + reconciliation_breaks with
    no log line naming the suspected cause — operator had to read the
    flex_query_fetcher source to triage.
    """

    async def test_warning_fires_when_backend_fut_but_no_broker_fut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        # Broker snapshot: ZERO positions (template missing FUT section).
        snap = _build_snapshot(
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)

        # Backend: one /M2K futures position (mirrors the 2026-05-27 EOD
        # break shape exactly).
        factory = _stub_session_factory(
            positions=[{"market": "/M2K", "qty": 1}],
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
                config=config, session_factory=factory, flex_client_factory=lambda: client
            )

        events = [c.get("event") for c in captured]
        assert "reconciliation_eod_cycle_broker_view_missing_futures" in events
        warning = next(
            c
            for c in captured
            if c.get("event") == "reconciliation_eod_cycle_broker_view_missing_futures"
        )
        assert warning["backend_futures_markets"] == ["/M2K"]
        assert warning["backend_futures_count"] == 1
        assert warning["broker_position_count"] == 0
        assert warning["log_level"] == "warning"
        # The hint string should name the suspected root cause so triage
        # is fast — operator can read structlog output + immediately know
        # to check the FlexQuery template config in IBKR portal.
        assert "FlexQuery template" in warning["hint"]
        assert "OpenPositions" in warning["hint"]

    async def test_warning_silent_when_broker_has_fut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Happy path: broker view has FUT rows (M2K), so the resilience
        # signal stays silent. We must not log on every healthy cycle.
        import structlog

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        snap = _build_snapshot(
            positions=(
                _flex_pos(symbol="M2KM6", sec_type="FUT", quantity="1", underlying_symbol="M2K"),
            ),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)
        factory = _stub_session_factory(
            positions=[{"market": "/M2K", "qty": 1}],
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
                config=config, session_factory=factory, flex_client_factory=lambda: client
            )

        events = [c.get("event") for c in captured]
        assert "reconciliation_eod_cycle_broker_view_missing_futures" not in events

    async def test_warning_silent_when_backend_has_no_fut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When backend has only ETFs (no FUT), the warning must NOT fire
        # even if broker view also has no FUT. (Backend has no FUT means
        # there's nothing for the FlexQuery template to omit; absence is
        # the correct behavior, not a misconfig.)
        import structlog

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
        )
        snap = _build_snapshot(
            positions=(_flex_pos(symbol="TLT", sec_type="STK", quantity="50"),),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("5000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)
        factory = _stub_session_factory(
            positions=[{"market": "TLT", "qty": 50}],
            balance={"cash_usd": Decimal("5000"), "net_liquidation": Decimal("10000")},
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
                config=config, session_factory=factory, flex_client_factory=lambda: client
            )

        events = [c.get("event") for c in captured]
        assert "reconciliation_eod_cycle_broker_view_missing_futures" not in events


class TestRunEodCycleReqPositionsSource:
    """Option C (2026-05-28) PR-B: the EOD cycle's position-quantity source
    is flag-selectable. ``position_source="reqpositions"`` swaps the broker
    view's positions from the FlexQuery XML snapshot (settlement-cleared, so
    same-day fills land after the clearing cutoff and trigger false-positive
    halts) onto IBKR's real-time ``reqPositions`` view, tagged
    ``BrokerSource.TWS_API``. Cash / NAV still come from FlexQuery. On a
    terminal ``reqPositions`` fetch failure the cycle falls back to FlexQuery
    positions and logs at ERROR — recon coverage is preserved, not skipped.
    """

    async def test_happy_path_with_reqpositions_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.reconciliation.eod_cycle as eod_cycle_mod

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
            position_source="reqpositions",
            ibkr_account_id="U25655583",
        )
        # FlexQuery snapshot DISAGREES on positions (/M2K qty 5) but agrees
        # on cash. reqPositions AGREES with backend (/M2K qty 1). If the
        # cycle had used FlexQuery for the position check we'd see a
        # position_qty break; the reqPositions override means we don't —
        # that's the Option C false-halt fix.
        snap = _build_snapshot(
            positions=(
                _flex_pos(symbol="M2KM6", sec_type="FUT", quantity="5", underlying_symbol="M2K"),
            ),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)

        factory = _stub_session_factory(
            positions=[{"market": "/M2K", "qty": 1}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        captured: dict[str, Any] = {}

        async def fake_fetch(**kwargs: Any) -> tuple[ReconPosition, ...]:
            captured["fetch_kwargs"] = kwargs
            return (ReconPosition(market="/M2K", quantity=Decimal("1")),)

        monkeypatch.setattr("services.reconciliation.eod_cycle.fetch_recon_positions", fake_fetch)

        # Wrap the real planner so we can inspect the broker_view it was
        # handed (source + positions) while still exercising real break
        # detection — the "no false break" assertion below is the point.
        real_plan = eod_cycle_mod.plan_reconciliation_check

        def capturing_plan(**kwargs: Any) -> Any:
            captured["broker_view"] = kwargs["broker_view"]
            return real_plan(**kwargs)

        monkeypatch.setattr(eod_cycle_mod, "plan_reconciliation_check", capturing_plan)

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        result = await run_eod_cycle(
            config=config, session_factory=factory, flex_client_factory=lambda: client
        )
        assert result is not None

        broker_view = captured["broker_view"]
        # reqPositions was the source → broker view tagged TWS_API.
        assert broker_view.source == BrokerSource.TWS_API
        # Positions came from reqPositions (/M2K qty 1), NOT the FlexQuery
        # snapshot (/M2K qty 5) — the override won.
        assert broker_view.positions == {"/M2K": Decimal("1")}
        # → no position_qty break despite FlexQuery disagreeing.
        assert len(captured["plan"].breaks_detected) == 0
        # fetch_recon_positions received the IBKR account NUMBER + gateway
        # params threaded through EodCycleConfig (not the backend UUID).
        assert captured["fetch_kwargs"]["account_id"] == "U25655583"
        assert captured["fetch_kwargs"]["host"] == "ib_gateway"
        assert captured["fetch_kwargs"]["port"] == 4004

    async def test_empty_reqpositions_with_backend_futures_degrades_no_halt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path B safety net (2026-05-29): a SUCCESSFUL-but-empty reqPositions
        while the backend holds futures is the cache-timing-race symptom — the
        exact 2026-05-29 false-halt recurrence. The cycle must NOT build a
        zero-broker view that trips a position_qty halt; it degrades to the
        FlexQuery position view + fires the P1 degraded alert
        (degrade_kind=empty_with_backend_futures)."""
        import structlog

        import services.reconciliation.eod_cycle as eod_cycle_mod

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
            position_source="reqpositions",
            ibkr_account_id="U25655583",
        )
        # FlexQuery snapshot DOES carry /M2K qty 1 (T+1 cleared, or never
        # lagged) — so the FlexQuery fallback broker view matches the backend
        # and NO break fires. Backend holds /M2K qty 1.
        snap = _build_snapshot(
            positions=(
                _flex_pos(symbol="M2KM6", sec_type="FUT", quantity="1", underlying_symbol="M2K"),
            ),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)

        factory = _stub_session_factory(
            positions=[{"market": "/M2K", "qty": 1}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        # reqPositions SUCCEEDS but returns EMPTY (the race after retries).
        async def fake_fetch(**kwargs: Any) -> tuple[ReconPosition, ...]:
            return ()

        monkeypatch.setattr("services.reconciliation.eod_cycle.fetch_recon_positions", fake_fetch)

        audit_payloads: list[dict[str, Any]] = []

        async def fake_audit(
            session: Any, event_type: Any, payload: dict[str, Any], **kwargs: Any
        ) -> MagicMock:
            audit_payloads.append(payload)
            return _audit_record_mock()

        monkeypatch.setattr("services.reconciliation.eod_cycle.append_audit_event", fake_audit)

        captured: dict[str, Any] = {}
        real_plan = eod_cycle_mod.plan_reconciliation_check

        def capturing_plan(**kwargs: Any) -> Any:
            captured["broker_view"] = kwargs["broker_view"]
            return real_plan(**kwargs)

        monkeypatch.setattr(eod_cycle_mod, "plan_reconciliation_check", capturing_plan)

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        hook_calls: list[AlertDispatchContext] = []

        async def hook(ctx: AlertDispatchContext) -> None:
            hook_calls.append(ctx)

        with structlog.testing.capture_logs() as logs:
            result = await run_eod_cycle(
                config=config,
                session_factory=factory,
                flex_client_factory=lambda: client,
                alert_dispatch_hook=hook,
            )
        assert result is not None

        # Degraded to FlexQuery: broker view is FlexQuery-sourced (NOT TWS_API)
        # and carries /M2K from the snapshot → matches backend → NO false break.
        broker_view = captured["broker_view"]
        assert broker_view.source == BrokerSource.FLEXQUERY_EOD
        assert broker_view.positions == {"/M2K": Decimal("1")}
        assert len(captured["plan"].breaks_detected) == 0

        # The empty-with-backend-futures degrade was logged + audited + alerted.
        events = [c.get("event") for c in logs]
        assert "eod_cycle_reqpositions_empty_with_backend_futures" in events
        degrade_payloads = [
            p for p in audit_payloads if p.get("degrade_kind") == "empty_with_backend_futures"
        ]
        assert len(degrade_payloads) == 1
        assert degrade_payloads[0]["degraded_source"] == "reqpositions"
        assert len(hook_calls) == 1
        assert hook_calls[0].descriptor.severity == "P1"
        assert hook_calls[0].descriptor.category == "reconciliation_data_source_degraded"

    async def test_empty_reqpositions_without_backend_futures_keeps_reqpositions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely flat account (backend has NO futures) + empty reqPositions
        is NOT degraded — an empty broker view that matches an empty backend is
        valid. The guard only fires when the backend actually holds futures."""
        import services.reconciliation.eod_cycle as eod_cycle_mod

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
            position_source="reqpositions",
            ibkr_account_id="U25655583",
        )
        snap = _build_snapshot(
            positions=(),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)
        factory = _stub_session_factory(
            positions=[],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        async def fake_fetch(**kwargs: Any) -> tuple[ReconPosition, ...]:
            return ()

        monkeypatch.setattr("services.reconciliation.eod_cycle.fetch_recon_positions", fake_fetch)

        captured: dict[str, Any] = {}
        real_plan = eod_cycle_mod.plan_reconciliation_check

        def capturing_plan(**kwargs: Any) -> Any:
            captured["broker_view"] = kwargs["broker_view"]
            return real_plan(**kwargs)

        monkeypatch.setattr(eod_cycle_mod, "plan_reconciliation_check", capturing_plan)

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        hook_calls: list[AlertDispatchContext] = []

        async def hook(ctx: AlertDispatchContext) -> None:
            hook_calls.append(ctx)

        result = await run_eod_cycle(
            config=config,
            session_factory=factory,
            flex_client_factory=lambda: client,
            alert_dispatch_hook=hook,
        )
        assert result is not None
        # No backend futures → empty reqPositions is a valid broker-flat view;
        # keep it tagged TWS_API and do NOT degrade / alert.
        assert captured["broker_view"].source == BrokerSource.TWS_API
        assert hook_calls == []

    async def test_reqpositions_failure_logs_and_falls_back_to_flexquery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        import services.reconciliation.eod_cycle as eod_cycle_mod

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="t",
            position_source="reqpositions",
            ibkr_account_id="U25655583",
        )
        # FlexQuery snapshot AGREES with backend (/M2K qty 1) so the
        # fallback path produces a clean, break-free cycle — proving recon
        # coverage is preserved when reqPositions is unavailable.
        snap = _build_snapshot(
            positions=(
                _flex_pos(symbol="M2KM6", sec_type="FUT", quantity="1", underlying_symbol="M2K"),
            ),
            cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
        )
        client = MagicMock()
        client.fetch_snapshot = AsyncMock(return_value=snap)

        factory = _stub_session_factory(
            positions=[{"market": "/M2K", "qty": 1}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        captured: dict[str, Any] = {}

        async def fake_fetch(**kwargs: Any) -> tuple[ReconPosition, ...]:
            raise ReconPositionsFetchError(
                operation="connect",
                detail="recon ib_gateway connect failed: gateway down",
                underlying_exception_class="IbkrPlacementError",
            )

        monkeypatch.setattr("services.reconciliation.eod_cycle.fetch_recon_positions", fake_fetch)

        # Option C recon-fix follow-up (2026-05-29): the fallback path now
        # writes a RECONCILIATION_DATA_SOURCE_DEGRADED audit row via the
        # degraded-source alert helper. Stub the writer so this test stays
        # focused on the fallback + ERROR-log contract (the audit/alert
        # contract is covered by TestRunEodCycleDataSourceDegradedAlert).
        # No alert_dispatch_hook is passed → the helper logs
        # ``eod_cycle_data_source_degraded_alert_skipped_no_hook`` + returns.
        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.append_audit_event",
            AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        )

        real_plan = eod_cycle_mod.plan_reconciliation_check

        def capturing_plan(**kwargs: Any) -> Any:
            captured["broker_view"] = kwargs["broker_view"]
            return real_plan(**kwargs)

        monkeypatch.setattr(eod_cycle_mod, "plan_reconciliation_check", capturing_plan)

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            captured["plan"] = plan
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        with structlog.testing.capture_logs() as logs:
            result = await run_eod_cycle(
                config=config, session_factory=factory, flex_client_factory=lambda: client
            )

        assert result is not None
        broker_view = captured["broker_view"]
        # Fell back to FlexQuery positions → broker view tagged FLEXQUERY_EOD.
        assert broker_view.source == BrokerSource.FLEXQUERY_EOD
        assert broker_view.positions == {"/M2K": Decimal("1")}
        # FlexQuery agreed with backend → no break (recon coverage preserved).
        assert len(captured["plan"].breaks_detected) == 0
        # The failure is surfaced at ERROR with the underlying cause + the
        # fact that we reverted to the known-broken source.
        failed = next(c for c in logs if c.get("event") == "eod_cycle_reqpositions_failed")
        assert failed["log_level"] == "error"
        assert failed["operation"] == "connect"
        assert failed["reason"] == "recon ib_gateway connect failed: gateway down"
        assert failed["underlying_exception_class"] == "IbkrPlacementError"
        assert failed["fallback"] == "flexquery"

    async def test_position_source_selected_log_line_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        # Default config → source "flexquery". The selection log fires at
        # cycle start on every run so the operator can confirm which source
        # produced the position check from the structlog stream alone.
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
            positions=[{"market": "/MES", "qty": 1}],
            balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
        )

        async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.kill_switch_invoked = False
            return mock_result

        monkeypatch.setattr(
            "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
        )

        with structlog.testing.capture_logs() as logs:
            await run_eod_cycle(
                config=config, session_factory=factory, flex_client_factory=lambda: client
            )

        selected = next(c for c in logs if c.get("event") == "eod_cycle_position_source_selected")
        assert selected["source"] == "flexquery"
        assert selected["env"] == "paper"


def _degraded_inputs(monkeypatch: pytest.MonkeyPatch) -> tuple[EodCycleConfig, MagicMock, Any]:
    """Common fixtures for the degraded-source alert tests.

    A ``reqpositions`` cycle whose reqPositions fetch fails terminally, with
    a FlexQuery snapshot that AGREES with backend (/M2K qty 1) so the
    fallback produces a clean, break-free cycle. ``apply_reconciliation_plan``
    is stubbed (no real DB writes, no break alerts) so the ONLY alert-hook
    invocation under test is the degraded-source push. The real
    ``plan_reconciliation_check`` runs (pure policy). Returns
    ``(config, client, factory)``.
    """

    config = EodCycleConfig(
        account_id=uuid4(),
        env="paper",
        flex_query_id=1,
        flex_query_token="t",
        position_source="reqpositions",
        ibkr_account_id="U25655583",
    )
    snap = _build_snapshot(
        positions=(
            _flex_pos(symbol="M2KM6", sec_type="FUT", quantity="1", underlying_symbol="M2K"),
        ),
        cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
    )
    client = MagicMock()
    client.fetch_snapshot = AsyncMock(return_value=snap)
    factory = _stub_session_factory(
        positions=[{"market": "/M2K", "qty": 1}],
        balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
    )

    async def fake_fetch(**kwargs: Any) -> tuple[ReconPosition, ...]:
        raise ReconPositionsFetchError(
            operation="connect",
            detail="recon ib_gateway connect failed: gateway down",
            underlying_exception_class="IbkrPlacementError",
        )

    monkeypatch.setattr("services.reconciliation.eod_cycle.fetch_recon_positions", fake_fetch)

    async def fake_apply(plan: Any, **kwargs: Any) -> MagicMock:
        mock_result = MagicMock()
        mock_result.kill_switch_invoked = False
        return mock_result

    monkeypatch.setattr("services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply)

    return config, client, factory


class TestRunEodCycleDataSourceDegradedAlert:
    """Option C recon-fix follow-up (2026-05-29): a terminal reqPositions
    fetch failure now pages the operator instead of only landing in the
    structlog stream. ``run_eod_cycle`` writes a
    ``RECONCILIATION_DATA_SOURCE_DEGRADED`` audit row (audit-first per
    backend-spec §2.10.1) then dispatches a ``P1`` alert (Discord #alerts,
    category ``reconciliation_data_source_degraded``) through the existing
    :class:`AlertDispatchContext` seam. The emit is fully defensive — the
    FlexQuery fallback runs regardless of audit / alert outcome.
    """

    async def test_emits_audit_and_p1_alert_on_reqpositions_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, client, factory = _degraded_inputs(monkeypatch)

        audit_uuid = uuid4()
        audit_calls: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []

        async def fake_audit(
            session: Any, event_type: Any, payload: dict[str, Any], **kwargs: Any
        ) -> MagicMock:
            audit_calls.append((event_type, payload, kwargs))
            return _audit_record_mock(event_uuid=audit_uuid)

        monkeypatch.setattr("services.reconciliation.eod_cycle.append_audit_event", fake_audit)

        hook_calls: list[AlertDispatchContext] = []

        async def hook(ctx: AlertDispatchContext) -> None:
            hook_calls.append(ctx)

        result = await run_eod_cycle(
            config=config,
            session_factory=factory,
            flex_client_factory=lambda: client,
            alert_dispatch_hook=hook,
        )
        # FlexQuery fallback still produced a result.
        assert result is not None

        # Exactly one audit row, with the new event type + structured payload.
        assert len(audit_calls) == 1
        event_type, payload, kwargs = audit_calls[0]
        assert event_type == AuditEventType.RECONCILIATION_DATA_SOURCE_DEGRADED
        assert payload == {
            "degraded_source": "reqpositions",
            "fallback_source": "flexquery",
            "degrade_kind": "fetch_failed",
            "operation": "connect",
            "reason": "recon ib_gateway connect failed: gateway down",
            "underlying_exception_class": "IbkrPlacementError",
        }
        assert kwargs["account_id"] == config.account_id
        assert kwargs["env"] == "paper"
        assert kwargs["phase_at_emit"] == config.phase_at_emit

        # Exactly one alert: P1, new category, pointing at the audit row.
        assert len(hook_calls) == 1
        ctx = hook_calls[0]
        assert isinstance(ctx, AlertDispatchContext)
        assert ctx.descriptor.severity == "P1"
        assert ctx.descriptor.category == "reconciliation_data_source_degraded"
        assert ctx.descriptor.triggering_break_index == -1
        assert ctx.descriptor.payload == payload
        assert ctx.triggering_audit_event_uuid == audit_uuid
        assert ctx.account_id == config.account_id
        assert ctx.env == "paper"

    async def test_audit_written_before_alert_dispatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, client, factory = _degraded_inputs(monkeypatch)
        order: list[str] = []

        async def fake_audit(session: Any, *a: Any, **k: Any) -> MagicMock:
            order.append("audit")
            return _audit_record_mock()

        monkeypatch.setattr("services.reconciliation.eod_cycle.append_audit_event", fake_audit)

        async def hook(ctx: AlertDispatchContext) -> None:
            order.append("alert")

        await run_eod_cycle(
            config=config,
            session_factory=factory,
            flex_client_factory=lambda: client,
            alert_dispatch_hook=hook,
        )
        # Audit-first per backend-spec §2.10.1.
        assert order == ["audit", "alert"]

    async def test_no_hook_still_writes_audit_and_logs_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        config, client, factory = _degraded_inputs(monkeypatch)
        audit_calls: list[Any] = []

        async def fake_audit(session: Any, event_type: Any, *a: Any, **k: Any) -> MagicMock:
            audit_calls.append(event_type)
            return _audit_record_mock()

        monkeypatch.setattr("services.reconciliation.eod_cycle.append_audit_event", fake_audit)

        with structlog.testing.capture_logs() as logs:
            result = await run_eod_cycle(
                config=config,
                session_factory=factory,
                flex_client_factory=lambda: client,
            )  # no alert_dispatch_hook wired

        assert result is not None
        # The durable audit breadcrumb still lands even with no hook.
        assert audit_calls == [AuditEventType.RECONCILIATION_DATA_SOURCE_DEGRADED]
        skip = next(
            c
            for c in logs
            if c.get("event") == "eod_cycle_data_source_degraded_alert_skipped_no_hook"
        )
        assert skip["log_level"] == "warning"

    async def test_audit_write_failure_swallowed_fallback_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        config, client, factory = _degraded_inputs(monkeypatch)

        async def boom_audit(*a: Any, **k: Any) -> MagicMock:
            raise RuntimeError("audit chain advisory lock timeout")

        monkeypatch.setattr("services.reconciliation.eod_cycle.append_audit_event", boom_audit)

        hook_calls: list[AlertDispatchContext] = []

        async def hook(ctx: AlertDispatchContext) -> None:
            hook_calls.append(ctx)

        with structlog.testing.capture_logs() as logs:
            result = await run_eod_cycle(
                config=config,
                session_factory=factory,
                flex_client_factory=lambda: client,
                alert_dispatch_hook=hook,
            )

        # Fallback still ran → cycle completed.
        assert result is not None
        # Audit write failed → no alert dispatched (no triggering uuid to stamp).
        assert hook_calls == []
        failed = next(
            c for c in logs if c.get("event") == "eod_cycle_data_source_degraded_audit_write_failed"
        )
        assert failed["log_level"] == "error"
        # The original reqpositions-failure ERROR log is still present.
        assert any(c.get("event") == "eod_cycle_reqpositions_failed" for c in logs)

    async def test_alert_dispatch_failure_swallowed_fallback_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        config, client, factory = _degraded_inputs(monkeypatch)
        audit_uuid = uuid4()

        async def fake_audit(*a: Any, **k: Any) -> MagicMock:
            return _audit_record_mock(event_uuid=audit_uuid)

        monkeypatch.setattr("services.reconciliation.eod_cycle.append_audit_event", fake_audit)

        async def boom_hook(ctx: AlertDispatchContext) -> None:
            raise RuntimeError("discord webhook 503")

        with structlog.testing.capture_logs() as logs:
            result = await run_eod_cycle(
                config=config,
                session_factory=factory,
                flex_client_factory=lambda: client,
                alert_dispatch_hook=boom_hook,
            )

        # A Discord outage MUST NOT take down the cycle — fallback completed.
        assert result is not None
        failed = next(
            c
            for c in logs
            if c.get("event") == "eod_cycle_data_source_degraded_alert_dispatch_failed"
        )
        assert failed["log_level"] == "error"
        assert failed["audit_event_uuid"] == str(audit_uuid)


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
            session_date_et=date(2026, 5, 16),
        )
        assert result == ()

    @pytest.mark.asyncio
    async def test_populated_trades_map_to_dataclass(self) -> None:
        from datetime import date

        from services.reconciliation.eod_cycle import fetch_closed_trades_for_session_date

        trade_id = uuid4()
        signal_id = uuid4()
        opened_at = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
        closed_at = datetime(2026, 5, 16, 20, 30, tzinfo=UTC)
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
            session_date_et=date(2026, 5, 16),
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
        row.opened_at_utc = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
        row.closed_at_utc = datetime(2026, 5, 16, 20, 30, tzinfo=UTC)
        row.expected_entry_price = Decimal("85.40")
        row.expected_slippage_bps = Decimal("1.2")
        row.expected_at_utc = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)

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
            session_date_et=date(2026, 5, 16),
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
            snapshot_pulled_at_utc=datetime(2026, 5, 16, 22, 30, tzinfo=UTC),
        )
        # Zero rows inserted (no closed trades)
        assert count == 0
        # Audit event fired exactly once (the rollup breadcrumb)
        append.assert_awaited_once()
        from services.audit.event_types import AuditEventType

        assert append.await_args.args[1] == AuditEventType.ATTRIBUTION_ROLLUP_RECORDED
        # Payload carries the zero-aggregate
        payload = append.await_args.args[2]
        assert payload["trade_count"] == 0
        assert payload["long_count"] == 0
        assert payload["short_count"] == 0


class TestRunEodCycleAttributionIntegration:
    """Verify the attribution rollup is invoked at the end of run_eod_cycle."""

    @pytest.mark.asyncio
    async def test_attribution_helper_called_from_run_eod_cycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new wiring must call _emit_daily_attribution_rollup from
        inside run_eod_cycle after apply_reconciliation_plan succeeds."""
        from services.reconciliation import eod_cycle as mod
        from services.reconciliation.eod_cycle import EodCycleConfig

        # Mock the recon stages so we focus on attribution wiring
        snapshot_mock = MagicMock()
        snapshot_mock.pulled_at_utc = datetime(2026, 5, 16, 22, 30, tzinfo=UTC)
        snapshot_mock.positions = ()
        snapshot_mock.cash_balances = ()
        snapshot_mock.account_summary = MagicMock(
            cash_usd=Decimal("0"),
            net_liquidation_usd=Decimal("0"),
            stock_market_value_usd=Decimal("0"),
            bond_market_value_usd=Decimal("0"),
            futures_pnl_usd=Decimal("0"),
        )

        flex_client_mock = MagicMock()
        flex_client_mock.fetch_snapshot = AsyncMock(return_value=snapshot_mock)

        def factory() -> Any:
            return flex_client_mock

        # The autouse fixture _patch_refresh already no-ops refresh_backend_from_broker_snapshot
        # Stub build_backend_view + apply_reconciliation_plan since they need a real DB
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
        # Stub apply_reconciliation_plan to return a result
        from services.reconciliation.apply import ReconciliationApplyResult

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

        # Spy on the attribution helper
        emit_spy = AsyncMock(return_value=0)
        monkeypatch.setattr(mod, "_emit_daily_attribution_rollup", emit_spy)

        # Fake session factory
        @asynccontextmanager
        async def _session_cm() -> Any:
            yield MagicMock()

        session_factory = MagicMock(side_effect=lambda: _session_cm())

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="token",
        )
        await mod.run_eod_cycle(
            config=config,
            session_factory=session_factory,
            flex_client_factory=factory,
        )

        # Attribution helper was called
        emit_spy.assert_awaited_once()
        kwargs = emit_spy.await_args.kwargs
        assert kwargs["env"] == "paper"
        # snapshot_pulled_at_utc was forwarded
        assert kwargs["snapshot_pulled_at_utc"] == snapshot_mock.pulled_at_utc

    @pytest.mark.asyncio
    async def test_attribution_failure_does_not_fail_cycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If _emit_daily_attribution_rollup raises, run_eod_cycle still
        returns the recon apply result. PR-K is best-effort."""
        from services.reconciliation import eod_cycle as mod
        from services.reconciliation.eod_cycle import EodCycleConfig

        snapshot_mock = MagicMock()
        snapshot_mock.pulled_at_utc = datetime(2026, 5, 16, 22, 30, tzinfo=UTC)
        snapshot_mock.positions = ()
        snapshot_mock.cash_balances = ()
        snapshot_mock.account_summary = MagicMock(
            cash_usd=Decimal("0"),
            net_liquidation_usd=Decimal("0"),
            stock_market_value_usd=Decimal("0"),
            bond_market_value_usd=Decimal("0"),
            futures_pnl_usd=Decimal("0"),
        )

        flex_client_mock = MagicMock()
        flex_client_mock.fetch_snapshot = AsyncMock(return_value=snapshot_mock)

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
        from services.reconciliation.apply import ReconciliationApplyResult

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

        config = EodCycleConfig(
            account_id=uuid4(),
            env="paper",
            flex_query_id=1,
            flex_query_token="token",
        )
        # Cycle still returns successfully (apply_result)
        result = await mod.run_eod_cycle(
            config=config,
            session_factory=session_factory,
            flex_client_factory=lambda: flex_client_mock,
        )
        assert result is apply_result
