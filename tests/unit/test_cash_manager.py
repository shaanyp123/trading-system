"""Unit tests for services/risk/cash_manager.py (delta spec §3.6, DORMANT).

Pure-planner battery ([A22]: no DB, no venue):

* target math: margin + 25%-of-gross + headroom
* floor guard: a to_yield sweep NEVER takes visible equity below
  max(target, halt_floor + headroom) — the Jul-17 shape is unmakeable
* reclaim path: bounded by known USDC; unknown/malformed USDC ⇒ no
  blind reclaim
* fail-safe no-ops for every missing/malformed input
* min-sweep churn guard, both directions
* visible_usd_equity parity with the worker's equity_from_summary
* audit mapping: existing capital-event types + the payload
  discriminator; and the applier's baseline-pollution guard (no
  capital_events writes) via SQL-capture fake

Applier coverage uses a fake session factory capturing SQL — pinning
audit-first ordering, the cash_sweeps INSERT/UPDATE shape, and that a
venue-leg failure lands as a 'failed' row without raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import uuid4

from services.audit.event_types import AuditEventType
from services.execution.types import BrokerPosition, FuturesBalanceSummary
from services.risk.cash_manager import (
    SWEEP_AUDIT_KIND,
    CashManagerParams,
    CashSweepError,
    CashSweepPlan,
    compute_gross_notional,
    plan_daily_sweep,
    visible_usd_equity,
)
from services.signal.strategy_worker import equity_from_summary

NOW = datetime(2026, 7, 18, 0, 25, tzinfo=UTC)
PARAMS = CashManagerParams()


def _summary(
    *,
    total: str | None = "2251.38",
    cfm: str | None = None,
    cbi: str | None = None,
    initial_margin: str | None = "0",
) -> FuturesBalanceSummary:
    return FuturesBalanceSummary(
        total_usd_balance=Decimal(total) if total is not None else None,
        cbi_usd_balance=Decimal(cbi) if cbi is not None else None,
        cfm_usd_balance=Decimal(cfm) if cfm is not None else None,
        available_margin=None,
        initial_margin=Decimal(initial_margin) if initial_margin is not None else None,
        unrealized_pnl=None,
        daily_realized_pnl=None,
        liquidation_threshold=None,
        liquidation_buffer_amount=None,
        liquidation_buffer_percentage=None,
        snapshot_at_utc=NOW,
    )


def _position(*, product_id: str = "BIP-20DEC30-CDE", contracts: str, mark: str | None):
    return BrokerPosition(
        product_id=product_id,
        contracts=Decimal(contracts),
        entry_vwap=None,
        mark_price=Decimal(mark) if mark is not None else None,
        unrealized_pnl_usd=None,
    )


class TestVisibleEquityParity:
    def test_visible_equity_parity(self) -> None:
        """cash_manager.visible_usd_equity must stay tuple-for-tuple
        identical to strategy_worker.equity_from_summary (the duplicated
        boundary helper — see the module docstring)."""
        cases = [
            _summary(total="2251.38"),
            _summary(total=None, cfm="153.42", cbi="1397.01"),
            _summary(total=None, cfm="153.42", cbi=None),
            _summary(total=None, cfm=None, cbi=None),
        ]
        for summary in cases:
            assert visible_usd_equity(summary) == equity_from_summary(summary)


class TestGrossNotional:
    SIZES: ClassVar[dict[str, Decimal]] = {
        "BIP-20DEC30-CDE": Decimal("0.01"),
        "ETP-20DEC30-CDE": Decimal("0.1"),
    }

    def test_flat_book_is_zero(self) -> None:
        assert compute_gross_notional([], self.SIZES) == Decimal("0")

    def test_sums_abs_notional(self) -> None:
        positions = [
            _position(contracts="-2", mark="64000"),  # 2 x 0.01 x 64000 = 1280
            _position(product_id="ETP-20DEC30-CDE", contracts="4", mark="3400"),  # 1360
        ]
        assert compute_gross_notional(positions, self.SIZES) == Decimal("2640.00")

    def test_missing_mark_or_size_fails_safe(self) -> None:
        assert compute_gross_notional([_position(contracts="2", mark=None)], self.SIZES) is None
        assert (
            compute_gross_notional(
                [_position(product_id="UNKNOWN-CDE", contracts="2", mark="100")], self.SIZES
            )
            is None
        )

    def test_nonfinite_fails_safe(self) -> None:
        assert compute_gross_notional([_position(contracts="2", mark="NaN")], self.SIZES) is None


class TestPlanner:
    def test_within_band_no_op(self) -> None:
        # target = 0 + 0 + 750; guard = 2250; visible 2251.38 → excess 1.38 < 50.
        decision = plan_daily_sweep(
            summary=_summary(total="2251.38"),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=Decimal("3500"),
            params=PARAMS,
        )
        assert decision.plan is None
        assert decision.skip_reason == "within_band"

    def test_excess_sweeps_to_yield_respecting_floor_guard(self) -> None:
        # visible 4000, flat book: sweep floor = max(750, 2250) = 2250 →
        # to_yield 1750; post-sweep visible would be exactly the guard.
        decision = plan_daily_sweep(
            summary=_summary(total="4000"),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=Decimal("0"),
            params=PARAMS,
        )
        assert decision.plan is not None
        assert decision.plan.direction == "to_yield"
        assert decision.plan.amount_usd == Decimal("1750.00")
        assert decision.plan.floor_guard_usd == Decimal("2250")
        assert (
            decision.plan.visible_equity_usd - decision.plan.amount_usd
            >= decision.plan.floor_guard_usd
        )

    def test_margin_and_buffer_raise_the_kept_amount(self) -> None:
        # margin 500 + 25% x 2640 = 660 + headroom 750 → target 1910;
        # guard 2250 still binds; visible 5000 → sweep 2750.
        decision = plan_daily_sweep(
            summary=_summary(total="5000", initial_margin="500"),
            gross_notional_usd=Decimal("2640"),
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert decision.plan is not None
        assert decision.plan.cfm_target_usd == Decimal("1910.00")
        assert decision.plan.amount_usd == Decimal("2750.00")

    def test_target_above_guard_binds_instead(self) -> None:
        # margin 2000 + 25% x 4000 = 1000 + 750 → target 3750 > guard 2250.
        decision = plan_daily_sweep(
            summary=_summary(total="6000", initial_margin="2000"),
            gross_notional_usd=Decimal("4000"),
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert decision.plan is not None
        assert decision.plan.direction == "to_yield"
        assert decision.plan.amount_usd == Decimal("2250.00")  # 6000 - 3750

    def test_deficit_reclaims_bounded_by_usdc(self) -> None:
        # target = 1000 + 0.25*2000 + 750 = 2250; visible 1500 → deficit 750.
        decision = plan_daily_sweep(
            summary=_summary(total="1500", initial_margin="1000"),
            gross_notional_usd=Decimal("2000"),
            cbi_usdc_usd=Decimal("400"),
            params=PARAMS,
        )
        assert decision.plan is not None
        assert decision.plan.direction == "to_margin"
        assert decision.plan.amount_usd == Decimal("400.00")  # bounded by USDC

    def test_deficit_with_unknown_usdc_never_reclaims_blind(self) -> None:
        decision = plan_daily_sweep(
            summary=_summary(total="1500", initial_margin="1000"),
            gross_notional_usd=Decimal("2000"),
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert decision.plan is None
        assert decision.skip_reason == "reclaim_needed_usdc_unknown"

    def test_malformed_usdc_degrades_to_unknown(self) -> None:
        for garbage in (Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
            decision = plan_daily_sweep(
                summary=_summary(total="1500", initial_margin="1000"),
                gross_notional_usd=Decimal("2000"),
                cbi_usdc_usd=garbage,
                params=PARAMS,
            )
            assert decision.plan is None
            assert decision.skip_reason == "reclaim_needed_usdc_unknown"

    def test_subcent_excess_rounds_down_never_breaching_guard(self) -> None:
        """Risk-review finding 3: HALF_EVEN could round a sweep UP by a
        sub-cent, landing post-sweep visible equity BELOW the guard.
        ROUND_DOWN pins the invariant at sub-cent inputs."""
        decision = plan_daily_sweep(
            summary=_summary(total="4000.006"),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert decision.plan is not None
        assert decision.plan.amount_usd == Decimal("1750.00")  # not 1750.01
        assert (
            decision.plan.visible_equity_usd - decision.plan.amount_usd
            >= decision.plan.floor_guard_usd
        )

    def test_subcent_reclaim_rounds_down_never_over_asking_usdc(self) -> None:
        decision = plan_daily_sweep(
            summary=_summary(total="1500", initial_margin="1000"),
            gross_notional_usd=Decimal("2000"),
            cbi_usdc_usd=Decimal("400.006"),
            params=PARAMS,
        )
        assert decision.plan is not None
        assert decision.plan.direction == "to_margin"
        assert decision.plan.amount_usd == Decimal("400.00")  # never 400.01
        assert decision.plan.amount_usd <= Decimal("400.006")

    def test_min_sweep_churn_guard_both_directions(self) -> None:
        # Excess 49 < 50 → no-op.
        up = plan_daily_sweep(
            summary=_summary(total="2299"),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=Decimal("100"),
            params=PARAMS,
        )
        assert up.plan is None
        # Deficit 49 < 50 → no-op (target 750 flat, visible 701).
        down = plan_daily_sweep(
            summary=_summary(total="701"),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=Decimal("100"),
            params=PARAMS,
        )
        assert down.plan is None

    def test_missing_inputs_fail_safe(self) -> None:
        no_equity = plan_daily_sweep(
            summary=_summary(total=None, cfm=None, cbi=None),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert no_equity.skip_reason == "visible_equity_unavailable"

        no_gross = plan_daily_sweep(
            summary=_summary(),
            gross_notional_usd=None,
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert no_gross.skip_reason == "gross_notional_unavailable"

        no_margin = plan_daily_sweep(
            summary=_summary(total="5000", initial_margin=None),
            gross_notional_usd=Decimal("1000"),  # positions exist, margin unknown
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert no_margin.skip_reason == "initial_margin_unavailable"

    def test_flat_book_missing_margin_is_zero(self) -> None:
        decision = plan_daily_sweep(
            summary=_summary(total="4000", initial_margin=None),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=None,
            params=PARAMS,
        )
        assert decision.plan is not None
        assert decision.plan.initial_margin_usd == Decimal("0")


class TestAuditMapping:
    def _plan(self, direction: str) -> CashSweepPlan:
        return CashSweepPlan(
            direction=direction,  # type: ignore[arg-type]
            amount_usd=Decimal("100.00"),
            visible_equity_usd=Decimal("3000"),
            cfm_target_usd=Decimal("2250"),
            floor_guard_usd=Decimal("2250"),
            initial_margin_usd=Decimal("0"),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=Decimal("3500"),
            reason="test",
        )

    def test_existing_taxonomy_reused_no_new_enums(self) -> None:
        assert self._plan("to_yield").audit_event_type() is AuditEventType.CAPITAL_EVENT_WITHDRAWAL
        assert self._plan("to_margin").audit_event_type() is AuditEventType.CAPITAL_EVENT_DEPOSIT

    def test_payload_discriminator_and_a05(self) -> None:
        payload = self._plan("to_yield").audit_payload(now_utc=NOW)
        assert payload["kind"] == SWEEP_AUDIT_KIND
        assert payload["internal_transfer"] is True
        assert payload["not_a_capital_event"] is True
        # [A05]: every money value serialized as a string, never float.
        for key in (
            "amount_usd",
            "visible_equity_usd",
            "cfm_target_usd",
            "floor_guard_usd",
            "initial_margin_usd",
            "gross_notional_usd",
            "cbi_usdc_usd",
        ):
            assert isinstance(payload[key], str)


# ---------------------------------------------------------------------------
# Applier — fake session factory capturing SQL
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    row: Any = None

    def fetchone(self) -> Any:
        return self.row


class _FakeSession:
    def __init__(self, sink: list[tuple[str, dict[str, Any]]]) -> None:
        self._sink = sink

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement)
        self._sink.append((sql, params or {}))
        if "INSERT INTO cash_sweeps" in sql:
            return _FakeResult(row=type("Row", (), {"id": 41})())
        return _FakeResult()

    def begin(self) -> _FakeSession:
        return self

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeSessionFactory:
    sink: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.sink)


class _FakeVenueClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def convert_cash(
        self, *, from_currency: str, to_currency: str, amount_usd: Decimal
    ) -> str:
        self.calls.append(
            ("convert", {"from": from_currency, "to": to_currency, "amount": amount_usd})
        )
        if self.fail:
            raise CashSweepError(operation="create_convert_quote", detail="venue down")
        return "trade-1"

    async def schedule_futures_sweep(self, *, amount_usd: Decimal) -> None:
        self.calls.append(("sweep", {"amount": amount_usd}))


class TestApplier:
    def _plan(self, direction: str = "to_yield") -> CashSweepPlan:
        return CashSweepPlan(
            direction=direction,  # type: ignore[arg-type]
            amount_usd=Decimal("1750.00"),
            visible_equity_usd=Decimal("4000"),
            cfm_target_usd=Decimal("2250"),
            floor_guard_usd=Decimal("2250"),
            initial_margin_usd=Decimal("0"),
            gross_notional_usd=Decimal("0"),
            cbi_usdc_usd=Decimal("3500"),
            reason="test",
        )

    async def _apply(self, *, direction: str = "to_yield", fail: bool = False):
        import services.risk.cash_manager as cm

        factory = _FakeSessionFactory()
        client = _FakeVenueClient(fail=fail)
        audit_calls: list[tuple[Any, dict[str, Any]]] = []

        async def fake_append(session: Any, event_type: Any, payload: Any, **kwargs: Any):
            audit_calls.append((event_type, payload))
            return type("Rec", (), {"event_uuid": uuid4()})()

        original = cm.append_audit_event
        cm.append_audit_event = fake_append  # type: ignore[assignment]
        try:
            result = await cm.apply_cash_sweep(
                factory,  # type: ignore[arg-type]
                plan=self._plan(direction),
                account_id=uuid4(),
                env="paper",
                phase_at_emit=1,
                client=client,
                now_utc=NOW,
            )
        finally:
            cm.append_audit_event = original  # type: ignore[assignment]
        return result, factory, client, audit_calls

    async def test_happy_path_audit_first_then_row_then_venue(self) -> None:
        result, factory, client, audit_calls = await self._apply()
        assert result.status == "completed"
        assert result.sweep_row_id == 41
        # Audit event happened, with the discriminator payload.
        assert len(audit_calls) == 1
        assert audit_calls[0][1]["kind"] == SWEEP_AUDIT_KIND
        # SQL: exactly one cash_sweeps INSERT + one terminal UPDATE —
        # and NEVER a capital_events write (baseline-pollution guard).
        sqls = [sql for sql, _ in factory.sink]
        assert sum("INSERT INTO cash_sweeps" in s for s in sqls) == 1
        assert sum("UPDATE cash_sweeps" in s for s in sqls) == 1
        assert not any("capital_events" in s for s in sqls)
        assert not any("risk_state" in s for s in sqls)
        # to_yield = one USD→USDC conversion, no futures sweep needed.
        assert [name for name, _ in client.calls] == ["convert"]
        assert client.calls[0][1]["from"] == "USD"

    async def test_to_margin_converts_then_schedules_sweep(self) -> None:
        result, _factory, client, _ = await self._apply(direction="to_margin")
        assert result.status == "completed"
        assert [name for name, _ in client.calls] == ["convert", "sweep"]
        assert client.calls[0][1]["from"] == "USDC"

    async def test_venue_failure_lands_failed_row_no_raise(self) -> None:
        result, factory, _client, audit_calls = await self._apply(fail=True)
        assert result.status == "failed"
        assert result.failure_detail is not None
        assert len(audit_calls) == 1  # audit landed before the venue leg
        update = next(p for s, p in factory.sink if "UPDATE cash_sweeps" in s)
        assert update["status"] == "failed"
        assert update["completed_at"] is None


class TestDormantWiring:
    async def test_lifespan_starter_early_returns_when_disabled(self, monkeypatch: Any) -> None:
        """The default configuration constructs NOTHING: no scheduler,
        no venue client, no task."""
        # Env BEFORE the imports: services.api.main builds the app (and
        # therefore APISettings) at module import time.
        monkeypatch.setenv("API_DATABASE_URL", "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub")
        from services.api.config import APISettings
        from services.api.main import _start_cash_manager

        settings = APISettings()  # type: ignore[call-arg]
        assert settings.cash_manager_enabled is False
        assert await _start_cash_manager(settings) is None


def test_job_never_raises_on_input_failure() -> None:
    """CashManagerJob.run_once swallows input-read failures (fail-safe:
    skip the day, never touch the book)."""
    import asyncio

    from services.risk.cash_manager import CashManagerJob

    class _BrokenBroker:
        async def get_futures_balance_summary(self) -> FuturesBalanceSummary:
            raise RuntimeError("venue down")

        async def list_positions(self) -> list[BrokerPosition]:
            return []

    class _AccountSession(_FakeSession):
        async def execute(self, statement: Any, params: dict[str, Any] | None = None):
            self._sink.append((str(statement), params or {}))
            if "FROM accounts" in str(statement):
                return _FakeResult(row=type("Row", (), {"id": uuid4()})())
            return _FakeResult()

    sink: list[tuple[str, dict[str, Any]]] = []

    class _Factory:
        def __call__(self) -> _AccountSession:
            return _AccountSession(sink)

    job = CashManagerJob(
        broker=_BrokenBroker(),
        sweep_client=_FakeVenueClient(),
        session_factory=_Factory(),  # type: ignore[arg-type]
        env="paper",
        phase_at_emit=1,
    )
    asyncio.run(job.run_once(date(2026, 7, 18)))  # must not raise
    assert not any("cash_sweeps" in sql for sql, _ in sink)
