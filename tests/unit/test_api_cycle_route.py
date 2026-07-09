"""Unit tests for ``GET /api/system/cycle`` (crypto-pivot §3.7).

Route-level coverage via the conftest ASGI app + stubbed repos (same
pattern as ``test_api_phase1_routes.py`` — no testcontainers):

  * empty DB — no active account → decision/worker null, 200
  * account with no worker rows → decision/worker null, 200
  * populated completed decision → per-asset score → target → action →
    costs projected; **Decimal-as-string on the wire** ([A05])
  * skipped decision → ``skip_reason`` surfaced, empty asset list
  * heartbeat freshness — fresh vs stale classification
  * corrupt outcome JSONB → degrades to empty assets (no 500)

Plus pure-function coverage:

  * :func:`services.api.routes.system.next_friday_cde_close_utc` —
    DST both ways (EDT 21:00Z / EST 22:00Z), boundary roll, [A06] naive
    rejection
  * :func:`services.api.repos.cycle._coerce_outcome` — dict / JSON
    string / corrupt payloads
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from services.api.repos.cycle import (
    StrategyDecisionRow,
    WorkerStatusRow,
    _coerce_outcome,
)
from services.api.routes import system as system_route
from services.api.routes.system import next_friday_cde_close_utc
from services.api.schemas.cycle import HEARTBEAT_FRESH_THRESHOLD_S

# ---------------------------------------------------------------------------
# Stub repos
# ---------------------------------------------------------------------------


@dataclass
class _StubPhase1Repo:
    """Minimal Phase1QueryRepo stub — the route only calls one method."""

    account_id: UUID | None = None

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account_id


@dataclass
class _StubCycleRepo:
    decision: StrategyDecisionRow | None = None
    status: WorkerStatusRow | None = None
    seen_account_ids: list[UUID] = field(default_factory=list)

    async def fetch_latest_decision(self, account_id: UUID) -> StrategyDecisionRow | None:
        self.seen_account_ids.append(account_id)
        return self.decision

    async def fetch_worker_status(self, account_id: UUID) -> WorkerStatusRow | None:
        return self.status


NOW = datetime(2026, 7, 9, 0, 12, tzinfo=UTC)


def _completed_decision_row() -> StrategyDecisionRow:
    """Mirror of the worker's ``strategy_decision_v1`` outcome shape.

    Per-asset values match what ``strategy_worker._run_decision`` writes:
    ``row.trend`` as a rounded float, ``est_cost_usd``/``mark`` as
    strings, ``stop`` filled after dispatch.
    """
    outcome = {
        "schema_version": "strategy_decision_v1",
        "decided_bar_date": "2026-07-08",
        "assets": {
            "BTC": {
                "row": {"trend": 0.421337, "sigma_ann": 0.55, "close": 64000.0},
                "engine_target": 2,
                "sized_target": 2,
                "final_target": 2,
                "prior_contracts": 0,
                "planned_delta": 2,
                "action": "buy",
                "legs": [{"seq": 0, "kind": "open", "delta": 2, "status": "filled"}],
                "est_cost_usd": "1.23",
                "mark": "64000.50",
                "final_contracts": 2,
                "stop": {"client_stop_level": "58100.00", "native_stop_order_id": "abc"},
            },
            "ETH": {
                "row": {"trend": -0.05, "close": 3400.0},
                "engine_target": 0,
                "sized_target": 0,
                "final_target": 0,
                "prior_contracts": 0,
                "planned_delta": 0,
                "action": "hold",
                "legs": [],
                "est_cost_usd": None,
                "mark": "3400.10",
            },
        },
        "weekly_halved": False,
        "m_combined": "1.0",
    }
    return StrategyDecisionRow(
        decision_date=date(2026, 7, 9),
        decided_at_utc=datetime(2026, 7, 9, 0, 5, 12, tzinfo=UTC),
        status="completed",
        env="paper",
        equity_usd=Decimal("2000.00"),
        equity_for_sizing_usd=Decimal("1500.00"),
        v_target=Decimal("0.80"),
        m_combined=Decimal("1.0"),
        weekly_halved=False,
        outcome=outcome,
        updated_at_utc=datetime(2026, 7, 9, 0, 7, tzinfo=UTC),
    )


def _skipped_decision_row() -> StrategyDecisionRow:
    return StrategyDecisionRow(
        decision_date=date(2026, 7, 9),
        decided_at_utc=datetime(2026, 7, 9, 0, 5, tzinfo=UTC),
        status="skipped_risk_state",
        env="paper",
        equity_usd=None,
        equity_for_sizing_usd=None,
        v_target=None,
        m_combined=None,
        weekly_halved=False,
        outcome={
            "schema_version": "strategy_decision_v1",
            "skip_reason": "risk_state=HALT_NEW does not permit dispatch",
        },
        updated_at_utc=datetime(2026, 7, 9, 0, 5, tzinfo=UTC),
    )


def _worker_status_row(*, heartbeat_age_s: int, marks_stale: bool = False) -> WorkerStatusRow:
    now = datetime.now(tz=UTC)
    return WorkerStatusRow(
        risk_loop_heartbeat_utc=now - timedelta(seconds=heartbeat_age_s),
        risk_loop_tick_count=2880,
        marks_stale=marks_stale,
        last_decision_date=date(2026, 7, 9),
        updated_at_utc=now,
    )


def _wire(
    override_dep: Any,
    *,
    account_id: UUID | None,
    decision: StrategyDecisionRow | None = None,
    status: WorkerStatusRow | None = None,
) -> _StubCycleRepo:
    cycle_repo = _StubCycleRepo(decision=decision, status=status)
    override_dep(system_route._get_repo, lambda: _StubPhase1Repo(account_id=account_id))
    override_dep(system_route._get_cycle_repo, lambda: cycle_repo)
    return cycle_repo


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


class TestSystemCycleRoute:
    async def test_no_active_account_returns_nulls(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        repo = _wire(override_dep, account_id=None)
        resp = await api_client.get("/api/system/cycle")
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] is None
        assert body["worker"] is None
        assert body["next_friday_close_utc"] is not None
        assert body["server_now"] is not None
        assert repo.seen_account_ids == []  # cycle repo never queried

    async def test_account_but_empty_tables_returns_nulls(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        account = uuid4()
        repo = _wire(override_dep, account_id=account)
        resp = await api_client.get("/api/system/cycle")
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] is None
        assert body["worker"] is None
        assert repo.seen_account_ids == [account]

    async def test_populated_decision_projects_assets(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(
            override_dep,
            account_id=uuid4(),
            decision=_completed_decision_row(),
            status=_worker_status_row(heartbeat_age_s=10),
        )
        resp = await api_client.get("/api/system/cycle")
        assert resp.status_code == 200
        decision = resp.json()["decision"]
        assert decision["decision_date"] == "2026-07-09"
        assert decision["status"] == "completed"
        assert decision["env"] == "paper"
        assert decision["skip_reason"] is None

        assets = decision["assets"]
        assert [a["asset"] for a in assets] == ["BTC", "ETH"]  # sorted
        btc = assets[0]
        assert btc["prior_contracts"] == 0
        assert btc["engine_target"] == 2
        assert btc["final_target"] == 2
        assert btc["action"] == "buy"
        eth = assets[1]
        assert eth["action"] == "hold"
        assert eth["est_cost_usd"] is None

    async def test_decimal_as_string_on_the_wire(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """[A05] — every money/ratio value serializes as a JSON string."""
        _wire(
            override_dep,
            account_id=uuid4(),
            decision=_completed_decision_row(),
        )
        resp = await api_client.get("/api/system/cycle")
        decision = resp.json()["decision"]
        assert decision["equity_usd"] == "2000.00"
        assert isinstance(decision["equity_usd"], str)
        assert decision["m_combined"] == "1.0"
        btc = decision["assets"][0]
        assert btc["trend_score"] == "0.421337"
        assert isinstance(btc["trend_score"], str)
        assert btc["est_cost_usd"] == "1.23"
        assert isinstance(btc["est_cost_usd"], str)
        assert btc["mark"] == "64000.50"
        assert btc["stop_level"] == "58100.00"

    async def test_skipped_decision_surfaces_skip_reason(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, account_id=uuid4(), decision=_skipped_decision_row())
        resp = await api_client.get("/api/system/cycle")
        decision = resp.json()["decision"]
        assert decision["status"] == "skipped_risk_state"
        assert decision["skip_reason"] == "risk_state=HALT_NEW does not permit dispatch"
        assert decision["assets"] == []
        assert decision["equity_usd"] is None

    async def test_fresh_heartbeat_classified_fresh(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(
            override_dep,
            account_id=uuid4(),
            status=_worker_status_row(heartbeat_age_s=10),
        )
        resp = await api_client.get("/api/system/cycle")
        worker = resp.json()["worker"]
        assert worker["heartbeat_fresh"] is True
        assert 0 <= worker["seconds_since_heartbeat"] <= HEARTBEAT_FRESH_THRESHOLD_S
        assert worker["risk_loop_tick_count"] == 2880
        assert worker["marks_stale"] is False
        assert worker["last_decision_date"] == "2026-07-09"

    async def test_stale_heartbeat_classified_stale(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(
            override_dep,
            account_id=uuid4(),
            status=_worker_status_row(heartbeat_age_s=600, marks_stale=True),
        )
        resp = await api_client.get("/api/system/cycle")
        worker = resp.json()["worker"]
        assert worker["heartbeat_fresh"] is False
        assert worker["seconds_since_heartbeat"] > HEARTBEAT_FRESH_THRESHOLD_S
        assert worker["marks_stale"] is True

    async def test_corrupt_outcome_degrades_to_empty_assets(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        row = _completed_decision_row()
        corrupt = StrategyDecisionRow(
            decision_date=row.decision_date,
            decided_at_utc=row.decided_at_utc,
            status=row.status,
            env=row.env,
            equity_usd=row.equity_usd,
            equity_for_sizing_usd=row.equity_for_sizing_usd,
            v_target=row.v_target,
            m_combined=row.m_combined,
            weekly_halved=row.weekly_halved,
            outcome={"assets": ["not", "a", "dict"], "skip_reason": 42},
            updated_at_utc=row.updated_at_utc,
        )
        _wire(override_dep, account_id=uuid4(), decision=corrupt)
        resp = await api_client.get("/api/system/cycle")
        assert resp.status_code == 200
        decision = resp.json()["decision"]
        assert decision["assets"] == []
        assert decision["skip_reason"] is None  # non-str degraded to None


# ---------------------------------------------------------------------------
# next_friday_cde_close_utc
# ---------------------------------------------------------------------------


class TestNextFridayCdeClose:
    def test_summer_edt_maps_to_2100z(self) -> None:
        # Thursday 2026-07-09 12:00Z → Friday 2026-07-10 17:00 EDT = 21:00Z
        now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
        assert next_friday_cde_close_utc(now) == datetime(2026, 7, 10, 21, 0, tzinfo=UTC)

    def test_winter_est_maps_to_2200z(self) -> None:
        # Wednesday 2026-01-14 12:00Z → Friday 2026-01-16 17:00 EST = 22:00Z
        now = datetime(2026, 1, 14, 12, 0, tzinfo=UTC)
        assert next_friday_cde_close_utc(now) == datetime(2026, 1, 16, 22, 0, tzinfo=UTC)

    def test_friday_morning_returns_same_day(self) -> None:
        now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)  # Friday 08:00 ET
        assert next_friday_cde_close_utc(now) == datetime(2026, 7, 10, 21, 0, tzinfo=UTC)

    def test_exactly_at_close_rolls_to_next_week(self) -> None:
        now = datetime(2026, 7, 10, 21, 0, tzinfo=UTC)  # Friday 17:00 ET sharp
        assert next_friday_cde_close_utc(now) == datetime(2026, 7, 17, 21, 0, tzinfo=UTC)

    def test_just_after_close_rolls_to_next_week(self) -> None:
        now = datetime(2026, 7, 10, 21, 0, 1, tzinfo=UTC)
        assert next_friday_cde_close_utc(now) == datetime(2026, 7, 17, 21, 0, tzinfo=UTC)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            next_friday_cde_close_utc(datetime(2026, 7, 9, 12, 0))


# ---------------------------------------------------------------------------
# repo outcome coercion
# ---------------------------------------------------------------------------


class TestCoerceOutcome:
    def test_dict_passthrough(self) -> None:
        assert _coerce_outcome({"a": 1}) == {"a": 1}

    def test_json_string_parsed(self) -> None:
        assert _coerce_outcome('{"a": 1}') == {"a": 1}

    def test_corrupt_string_degrades_to_empty(self) -> None:
        assert _coerce_outcome("{not json") == {}

    def test_non_object_degrades_to_empty(self) -> None:
        assert _coerce_outcome([1, 2]) == {}
        assert _coerce_outcome(None) == {}
        assert _coerce_outcome("[1, 2]") == {}
