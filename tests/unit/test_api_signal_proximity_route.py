"""Unit tests for ``GET /api/signals/proximity`` (crypto-pivot §3.9).

The entry-side "Watching" projection: per-asset hysteresis-flip
proximity composed from the strategy worker's persisted
``engine_state`` + the latest decision outcome + the active parameter
set's HYSTERESIS_DAYS.

Route-level coverage via the conftest ASGI app + stubbed repos (same
pattern as ``test_api_cycle_route.py`` — no testcontainers):

  * empty DB — no active account → ``{"assets": []}``, 200
  * account with no worker row → empty assets, 200
  * populated engine state → hysteresis math, lockout, vol-block,
    trend score + decision-time mark; **Decimal-as-string** ([A05])
  * HYSTERESIS_DAYS from the active parameter set vs the Amendment B
    canonical fallback

Plus pure-function coverage:

  * :func:`days_to_flip` — pending vs steady vs same-direction cases
  * :func:`hysteresis_days_from_parameters` — parse + fallback branches
  * :func:`build_signal_proximity_response` — [A06] naive rejection
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from services.api.repos.cycle import StrategyDecisionRow, WorkerStatusRow
from services.api.repos.phase1 import ParameterSetRow
from services.api.routes import signals as signals_route
from services.api.schemas.signal_proximity import (
    FALLBACK_HYSTERESIS_DAYS,
    build_signal_proximity_response,
    days_to_flip,
    hysteresis_days_from_parameters,
)

# Midnight-UTC test bug (2026-07-10, C1 night one): a frozen NOW pinned
# "today" to 2026-07-09 while the route derives lockout arithmetic from
# the REAL clock — the test failed once the UTC calendar rolled. NOW
# tracks the live clock; date-relative fixtures stay correct at any hour.
NOW = datetime.now(tz=UTC)
TODAY_ORD = NOW.date().toordinal()

# ---------------------------------------------------------------------------
# Stub repos
# ---------------------------------------------------------------------------


@dataclass
class _StubPhase1Repo:
    account_id: UUID | None = None
    parameter_set: ParameterSetRow | None = None

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account_id

    async def fetch_active_parameter_set(self) -> ParameterSetRow | None:
        return self.parameter_set


@dataclass
class _StubCycleRepo:
    decision: StrategyDecisionRow | None = None
    status: WorkerStatusRow | None = None
    seen_account_ids: list[UUID] = field(default_factory=list)

    async def fetch_latest_decision(self, account_id: UUID) -> StrategyDecisionRow | None:
        return self.decision

    async def fetch_worker_status(self, account_id: UUID) -> WorkerStatusRow | None:
        self.seen_account_ids.append(account_id)
        return self.status


def _engine_state() -> dict[str, Any]:
    """AssetRuntime.to_json shape (Decimals as strings) for both assets.

    BTC: long, a short flip pending 1-of-2 bars. ETH: flat, vol-blocked,
    inside a 2-day lockout that fired on today's bar.
    """
    return {
        "BTC": {
            "contracts": 2,
            "applied_dir": 1,
            "pending_dir": -1,
            "pending_count": 1,
            "lockout_until_ord": -1,
            "lockout_dir": 0,
            "vol_blocked": False,
            "stopped_on_date": None,
            "entry_vwap": "63950.00",
            "client_stop_level": "58100.00",
            "atrp_at_stop_set": "0.045",
            "native_stop_order_id": "abc",
            "native_stop_client_order_id": "coid-btc-1",
            "stop_rearm_version": 1,
        },
        "ETH": {
            "contracts": 0,
            "applied_dir": 0,
            "pending_dir": 0,
            "pending_count": 0,
            "lockout_until_ord": TODAY_ORD + 2,
            "lockout_dir": 1,
            "vol_blocked": True,
            "stopped_on_date": NOW.date().isoformat(),
            "entry_vwap": None,
            "client_stop_level": None,
            "atrp_at_stop_set": None,
            "native_stop_order_id": None,
            "native_stop_client_order_id": None,
            "stop_rearm_version": 0,
        },
    }


def _worker_status(engine_state: dict[str, Any] | None = None) -> WorkerStatusRow:
    return WorkerStatusRow(
        risk_loop_heartbeat_utc=NOW,
        risk_loop_tick_count=100,
        marks_stale=False,
        last_decision_date=date(2026, 7, 9),
        updated_at_utc=NOW,
        engine_state=engine_state if engine_state is not None else _engine_state(),
    )


def _decision_row() -> StrategyDecisionRow:
    outcome = {
        "schema_version": "strategy_decision_v1",
        "decided_bar_date": "2026-07-08",
        "assets": {
            "BTC": {"row": {"trend": 0.421337}, "mark": "64000.50"},
            "ETH": {"row": {"trend": -0.05}, "mark": None},
        },
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


def _parameter_set(hysteresis: str) -> ParameterSetRow:
    return ParameterSetRow(
        parameter_set_hash="a" * 64,
        parameters={"HYSTERESIS_DAYS": hysteresis},
        first_active_at=datetime(2026, 7, 9, tzinfo=UTC),
        last_active_at=None,
    )


def _wire(
    override_dep: Any,
    *,
    account_id: UUID | None,
    status: WorkerStatusRow | None = None,
    decision: StrategyDecisionRow | None = None,
    parameter_set: ParameterSetRow | None = None,
) -> _StubCycleRepo:
    cycle_repo = _StubCycleRepo(decision=decision, status=status)
    override_dep(
        signals_route._get_repo,
        lambda: _StubPhase1Repo(account_id=account_id, parameter_set=parameter_set),
    )
    override_dep(signals_route._get_cycle_repo, lambda: cycle_repo)
    return cycle_repo


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


class TestSignalProximityRoute:
    async def test_no_active_account_returns_empty_assets(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        repo = _wire(override_dep, account_id=None)
        resp = await api_client.get("/api/signals/proximity")
        assert resp.status_code == 200
        body = resp.json()
        assert body["assets"] == []
        assert body["server_now"] is not None
        assert repo.seen_account_ids == []  # cycle repo never queried

    async def test_account_but_no_worker_state_returns_empty_assets(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        account = uuid4()
        repo = _wire(override_dep, account_id=account)
        resp = await api_client.get("/api/signals/proximity")
        assert resp.status_code == 200
        assert resp.json()["assets"] == []
        assert repo.seen_account_ids == [account]

    async def test_populated_engine_state_projects_assets(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(
            override_dep,
            account_id=uuid4(),
            status=_worker_status(),
            decision=_decision_row(),
        )
        resp = await api_client.get("/api/signals/proximity")
        assert resp.status_code == 200
        assets = resp.json()["assets"]
        assert [a["asset"] for a in assets] == ["BTC", "ETH"]  # sorted

        btc = assets[0]
        assert btc["current_dir"] == 1
        assert btc["pending_dir"] == -1
        assert btc["pending_count"] == 1
        # Amendment B fallback HYSTERESIS_DAYS=2, pending_count=1 → 1 day left.
        assert btc["hysteresis_days"] == FALLBACK_HYSTERESIS_DAYS
        assert btc["days_to_flip"] == FALLBACK_HYSTERESIS_DAYS - 1
        assert btc["lockout_active"] is False
        assert btc["lockout_days_left"] is None
        assert btc["vol_blocked"] is False
        assert btc["last_decision_date"] == "2026-07-09"

        eth = assets[1]
        assert eth["current_dir"] == 0
        assert eth["pending_dir"] is None  # engine 0 = no pending flip
        assert eth["days_to_flip"] is None
        assert eth["lockout_active"] is True
        assert eth["lockout_days_left"] == 2
        assert eth["vol_blocked"] is True
        assert eth["mark"] is None

    async def test_decimal_as_string_on_the_wire(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """[A05] — trend score + mark serialize as JSON strings."""
        _wire(
            override_dep,
            account_id=uuid4(),
            status=_worker_status(),
            decision=_decision_row(),
        )
        resp = await api_client.get("/api/signals/proximity")
        btc = resp.json()["assets"][0]
        assert btc["trend_score"] == "0.421337"
        assert isinstance(btc["trend_score"], str)
        assert btc["mark"] == "64000.50"
        assert isinstance(btc["mark"], str)

    async def test_hysteresis_days_from_active_parameter_set(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(
            override_dep,
            account_id=uuid4(),
            status=_worker_status(),
            parameter_set=_parameter_set("5"),
        )
        resp = await api_client.get("/api/signals/proximity")
        btc = resp.json()["assets"][0]
        assert btc["hysteresis_days"] == 5
        assert btc["days_to_flip"] == 4  # 5 - pending_count(1)

    async def test_no_decision_row_degrades_scores_to_null(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, account_id=uuid4(), status=_worker_status())
        resp = await api_client.get("/api/signals/proximity")
        assert resp.status_code == 200
        for asset in resp.json()["assets"]:
            assert asset["trend_score"] is None
            assert asset["mark"] is None


# ---------------------------------------------------------------------------
# Pure mappers
# ---------------------------------------------------------------------------


class TestDaysToFlip:
    def test_pending_flip_counts_down(self) -> None:
        assert days_to_flip(current_dir=1, pending_dir=-1, pending_count=1, hysteresis_days=2) == 1

    def test_no_pending_direction_is_steady(self) -> None:
        assert (
            days_to_flip(current_dir=1, pending_dir=None, pending_count=0, hysteresis_days=2)
            is None
        )

    def test_pending_equals_applied_is_steady(self) -> None:
        # Hysteresis-hold: a pending direction matching the applied one is
        # not a flip in progress.
        assert (
            days_to_flip(current_dir=-1, pending_dir=-1, pending_count=2, hysteresis_days=2) is None
        )

    def test_flip_due_today_is_zero(self) -> None:
        assert days_to_flip(current_dir=0, pending_dir=1, pending_count=2, hysteresis_days=2) == 0


class TestHysteresisDaysFromParameters:
    def test_reads_seeded_string_value(self) -> None:
        assert hysteresis_days_from_parameters({"HYSTERESIS_DAYS": "3"}) == 3

    def test_missing_row_falls_back(self) -> None:
        assert hysteresis_days_from_parameters(None) == FALLBACK_HYSTERESIS_DAYS

    def test_missing_key_falls_back(self) -> None:
        assert hysteresis_days_from_parameters({"V_TARGET": "0.80"}) == FALLBACK_HYSTERESIS_DAYS

    def test_unparseable_falls_back(self) -> None:
        assert (
            hysteresis_days_from_parameters({"HYSTERESIS_DAYS": "soon"}) == FALLBACK_HYSTERESIS_DAYS
        )

    def test_non_positive_falls_back(self) -> None:
        assert hysteresis_days_from_parameters({"HYSTERESIS_DAYS": "0"}) == FALLBACK_HYSTERESIS_DAYS


class TestBuildResponse:
    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            build_signal_proximity_response(
                worker=None,
                decision=None,
                hysteresis_days=2,
                now=datetime(2026, 7, 9, 12, 0),
            )

    def test_none_worker_yields_empty(self) -> None:
        resp = build_signal_proximity_response(
            worker=None, decision=None, hysteresis_days=2, now=NOW
        )
        assert resp.assets == []
        assert resp.server_now == NOW

    def test_non_mapping_engine_entries_skipped(self) -> None:
        status = _worker_status(engine_state={"BTC": "corrupt", "ETH": _engine_state()["ETH"]})
        resp = build_signal_proximity_response(
            worker=status, decision=None, hysteresis_days=2, now=NOW
        )
        assert [a.asset for a in resp.assets] == ["ETH"]
