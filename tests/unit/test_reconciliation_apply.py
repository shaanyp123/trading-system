"""Unit tests for ``services.reconciliation.apply.apply_reconciliation_plan``.

Worker-PR-3b (post-pivot 2026-05-12). These tests exercise the
orchestrator's audit-event sequencing + kill-switch hook firing +
positional pairing of audit_event_uuids with breaks/resolutions —
WITHOUT a real Postgres. The DB-touching path is covered by the
integration suite in tests/integration/test_reconciliation_apply.py
which runs against testcontainers.

A22 N/A — no Postgres in this file.
A06 enforced — every datetime tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.reconciliation.apply import (
    ReconciliationApplyResult,
    ReconciliationKillSwitchContext,
    apply_reconciliation_plan,
)
from services.reconciliation.recon import (
    BrokerSource,
    PendingAuditEvent,
    ReconciliationBreak,
    ReconciliationMetric,
    ReconciliationPlan,
    ResolvedPriorBreak,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditRecord:
    event_uuid: UUID
    sequence_no: int


def _make_break(
    *,
    metric: ReconciliationMetric = ReconciliationMetric.POSITION_QTY,
    market: str | None = "/MES",
    delta: Decimal = Decimal("1"),
    within_grace_period: bool = False,
) -> ReconciliationBreak:
    return ReconciliationBreak(
        metric=metric,
        market=market,
        expected=Decimal("1"),
        actual=Decimal("0"),
        delta=delta,
        tolerance=Decimal("0"),
        source=BrokerSource.TWS_API,
        detected_at_utc=datetime(2026, 5, 12, 18, 30, tzinfo=UTC),
        within_grace_period=within_grace_period,
        resolution_path=None,
    )


def _make_pending(
    event_type: str = "reconciliation_break_detected",
    **payload_overrides: Any,
) -> PendingAuditEvent:
    payload: dict[str, Any] = {"market": "/MES"}
    payload.update(payload_overrides)
    return PendingAuditEvent(
        event_type=event_type,  # type: ignore[arg-type]
        payload=payload,
    )


def _make_resolved(
    metric: ReconciliationMetric = ReconciliationMetric.POSITION_QTY,
    market: str | None = "/MES",
    delta: Decimal = Decimal("1"),
) -> ResolvedPriorBreak:
    return ResolvedPriorBreak(metric=metric, market=market, delta=delta)


class _FakeSessionFactory:
    """Async-context-manager session factory backing the apply tests.

    Returns a MagicMock session on each call. ``execute().fetchone()``
    returns a SimpleNamespace with ``.id`` set to a fresh UUID4 so
    `_insert_breaks` can read the returned id. `execute().rowcount`
    returns 1 for resolve UPDATEs so the test confirms the counter
    increments.
    """

    def __init__(self) -> None:
        self.created_sessions: list[MagicMock] = []

    def __call__(self) -> Any:
        session_cm = MagicMock()

        async def aenter() -> MagicMock:
            return session_cm

        async def aexit(*_: Any) -> None:
            return None

        async def commit_async() -> None:
            return None

        # Async context manager for `async with session_factory() as session:`
        session_cm.__aenter__ = lambda self_: aenter()
        session_cm.__aexit__ = lambda self_, *_: aexit()

        # Async context manager for `async with session.begin():`
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=begin_cm)
        begin_cm.__aexit__ = AsyncMock(return_value=None)
        session_cm.begin = MagicMock(return_value=begin_cm)
        session_cm.commit = commit_async

        # execute() returns an object with fetchone() yielding {id: UUID} +
        # rowcount=1.
        async def execute_async(stmt: Any, params: Any = None) -> Any:
            from types import SimpleNamespace

            result = MagicMock()
            new_id = uuid4()
            row = SimpleNamespace(id=new_id)
            result.fetchone = MagicMock(return_value=row)
            result.rowcount = 1
            return result

        session_cm.execute = execute_async
        # SQLAlchemy's `async with session_factory() as session:` returns
        # the *session*, not the async-ctx. Make the wrapper itself an
        # async-ctx that yields a session object.
        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=session_cm)
        outer.__aexit__ = AsyncMock(return_value=None)
        self.created_sessions.append(session_cm)
        return outer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_writer(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """Replace append_audit_event with a fake that captures + mints UUIDs."""
    captured: list[tuple[Any, Any]] = []

    async def fake_append(
        session: Any,
        event_type: Any,
        payload: Any,
        *,
        account_id: Any,
        env: Any,
        phase_at_emit: Any,
        source_clock_ts: Any = None,
    ) -> _FakeAuditRecord:
        captured.append((event_type, payload))
        return _FakeAuditRecord(event_uuid=uuid4(), sequence_no=len(captured))

    from services.reconciliation import apply as apply_mod

    monkeypatch.setattr(apply_mod, "append_audit_event", fake_append)
    return captured


class TestApplyEmptyPlan:
    @pytest.mark.asyncio
    async def test_no_events_no_breaks_returns_empty_result(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        plan = ReconciliationPlan(
            breaks_detected=(),
            breaks_resolved=(),
            audit_events=(),
            actionable_break_count=0,
            should_invoke_kill_switch=False,
        )
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
        )
        assert isinstance(result, ReconciliationApplyResult)
        assert result.audit_event_uuids == ()
        assert result.inserted_break_ids == ()
        assert result.resolved_break_count == 0
        assert result.kill_switch_invoked is False
        assert fake_writer == []


class TestApplyCheckPassed:
    @pytest.mark.asyncio
    async def test_check_passed_writes_one_audit_no_break_rows(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        plan = ReconciliationPlan(
            breaks_detected=(),
            breaks_resolved=(),
            audit_events=(_make_pending("reconciliation_check_passed"),),
            actionable_break_count=0,
            should_invoke_kill_switch=False,
        )
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
        )
        assert len(result.audit_event_uuids) == 1
        assert result.inserted_break_ids == ()
        assert len(fake_writer) == 1
        from services.audit.event_types import AuditEventType

        assert fake_writer[0][0] == AuditEventType.RECONCILIATION_CHECK_PASSED


class TestApplyBreakDetected:
    @pytest.mark.asyncio
    async def test_one_break_writes_audit_then_inserts_break_row(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        break_ = _make_break()
        plan = ReconciliationPlan(
            breaks_detected=(break_,),
            breaks_resolved=(),
            audit_events=(_make_pending("reconciliation_break_detected"),),
            actionable_break_count=1,
            should_invoke_kill_switch=False,
        )
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
        )
        assert len(result.audit_event_uuids) == 1
        assert len(result.inserted_break_ids) == 1
        assert len(fake_writer) == 1

    @pytest.mark.asyncio
    async def test_multiple_breaks_pair_positionally(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        b1 = _make_break(market="/MES", delta=Decimal("1"))
        b2 = _make_break(market="/MNQ", delta=Decimal("2"))
        plan = ReconciliationPlan(
            breaks_detected=(b1, b2),
            breaks_resolved=(),
            audit_events=(
                _make_pending("reconciliation_break_detected", market="/MES"),
                _make_pending("reconciliation_break_detected", market="/MNQ"),
            ),
            actionable_break_count=2,
            should_invoke_kill_switch=False,
        )
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
        )
        assert len(result.audit_event_uuids) == 2
        assert len(result.inserted_break_ids) == 2


class TestApplyKillSwitchHook:
    @pytest.mark.asyncio
    async def test_hook_fires_when_should_invoke_true(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        captured_ctxs: list[ReconciliationKillSwitchContext] = []

        async def hook(ctx: ReconciliationKillSwitchContext) -> None:
            captured_ctxs.append(ctx)

        plan = ReconciliationPlan(
            breaks_detected=(_make_break(),),
            breaks_resolved=(),
            audit_events=(_make_pending("reconciliation_break_detected"),),
            actionable_break_count=1,
            should_invoke_kill_switch=True,
        )
        account_id = uuid4()
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=account_id,
            env="paper",
            state_transition_hook=hook,
        )
        assert result.kill_switch_invoked is True
        assert len(captured_ctxs) == 1
        assert captured_ctxs[0].account_id == account_id
        assert captured_ctxs[0].env == "paper"
        assert captured_ctxs[0].actionable_break_count == 1
        assert captured_ctxs[0].primary_audit_event_uuid == result.audit_event_uuids[0]

    @pytest.mark.asyncio
    async def test_hook_not_fired_when_should_invoke_false(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        called = {"count": 0}

        async def hook(ctx: ReconciliationKillSwitchContext) -> None:
            called["count"] += 1

        plan = ReconciliationPlan(
            breaks_detected=(_make_break(within_grace_period=True),),
            breaks_resolved=(),
            audit_events=(_make_pending("reconciliation_break_detected"),),
            actionable_break_count=0,  # within grace; no actionable break
            should_invoke_kill_switch=False,
        )
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
            state_transition_hook=hook,
        )
        assert result.kill_switch_invoked is False
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_hook_omitted_skips_invocation_silently(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        """When should_invoke=True but no hook provided, just log + skip."""
        plan = ReconciliationPlan(
            breaks_detected=(_make_break(),),
            breaks_resolved=(),
            audit_events=(_make_pending("reconciliation_break_detected"),),
            actionable_break_count=1,
            should_invoke_kill_switch=True,
        )
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
            state_transition_hook=None,
        )
        assert result.kill_switch_invoked is False


class TestApplyResolvedBreaks:
    @pytest.mark.asyncio
    async def test_resolved_breaks_update_runs(self, fake_writer: list[tuple[Any, Any]]) -> None:
        plan = ReconciliationPlan(
            breaks_detected=(),
            breaks_resolved=(_make_resolved(),),
            audit_events=(_make_pending("reconciliation_break_resolved"),),
            actionable_break_count=0,
            should_invoke_kill_switch=False,
        )
        result = await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
        )
        # The fake session returns rowcount=1 per execute → resolved_break_count
        # equals the number of resolved tuples.
        assert result.resolved_break_count == 1

    @pytest.mark.asyncio
    async def test_resolution_path_must_be_in_check_constraint(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        """Default resolution_path is 'manual' (valid per CHECK)."""
        plan = ReconciliationPlan(
            breaks_detected=(),
            breaks_resolved=(_make_resolved(),),
            audit_events=(_make_pending("reconciliation_break_resolved"),),
            actionable_break_count=0,
            should_invoke_kill_switch=False,
        )
        # Doesn't raise — 'manual' is in the Literal.
        await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
            resolution_path="manual",
        )


class TestApplyOrdering:
    @pytest.mark.asyncio
    async def test_audit_events_written_in_declared_order(
        self, fake_writer: list[tuple[Any, Any]]
    ) -> None:
        plan = ReconciliationPlan(
            breaks_detected=(_make_break(), _make_break(market="/MNQ")),
            breaks_resolved=(_make_resolved(),),
            audit_events=(
                _make_pending("reconciliation_break_detected", market="/MES"),
                _make_pending("reconciliation_break_detected", market="/MNQ"),
                _make_pending("reconciliation_break_resolved", market="/MES"),
            ),
            actionable_break_count=2,
            should_invoke_kill_switch=False,
        )
        await apply_reconciliation_plan(
            plan,
            session_factory=_FakeSessionFactory(),
            account_id=uuid4(),
            env="paper",
        )
        assert [c[1].get("market") for c in fake_writer] == ["/MES", "/MNQ", "/MES"]
