"""Unit tests for the bar-sync status provider + ``GET /api/system/bar-sync``."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from services.api.bar_sync_status import (
    BarSyncStatusProvider,
    get_bar_sync_status_provider,
)
from services.api.routes.system import _get_bar_sync_status_source
from services.data.bar_sync import BarSyncCycleResult, MarketSyncResult

# ---------------------------------------------------------------------------
# Stub source (satisfies BarSyncStatusSource structurally)
# ---------------------------------------------------------------------------


class _StubSource:
    def __init__(
        self,
        *,
        last_cycle_result: BarSyncCycleResult | None,
        consecutive_failure_count: int = 0,
        consecutive_sentinel_count: int = 0,
        last_fired_session_date_et: date | None = None,
        is_running: bool = True,
    ) -> None:
        self._last = last_cycle_result
        self._fail = consecutive_failure_count
        self._sentinel = consecutive_sentinel_count
        self._fired = last_fired_session_date_et
        self._running = is_running

    @property
    def last_cycle_result(self) -> BarSyncCycleResult | None:
        return self._last

    @property
    def consecutive_failure_count(self) -> int:
        return self._fail

    @property
    def consecutive_sentinel_count(self) -> int:
        return self._sentinel

    @property
    def last_fired_session_date_et(self) -> date | None:
        return self._fired

    @property
    def is_running(self) -> bool:
        return self._running


def _ok_cycle() -> BarSyncCycleResult:
    return BarSyncCycleResult(
        cycle_started_at_utc=datetime(2026, 5, 19, 21, 0, 0, tzinfo=UTC),
        cycle_completed_at_utc=datetime(2026, 5, 19, 21, 0, 42, tzinfo=UTC),
        successful_markets=(
            MarketSyncResult(
                market="TLT",
                success=True,
                bars_written=250,
                last_session_date=date(2026, 5, 19),
                front_month_expiry=None,
                error=None,
                open_interest=None,
            ),
            MarketSyncResult(
                market="/MES",
                success=True,
                bars_written=250,
                last_session_date=date(2026, 5, 19),
                front_month_expiry="202606",
                error=None,
                open_interest=50000,
            ),
        ),
        failed_markets=(),
    )


def _dirty_cycle() -> BarSyncCycleResult:
    return BarSyncCycleResult(
        cycle_started_at_utc=datetime(2026, 5, 19, 21, 0, 0, tzinfo=UTC),
        cycle_completed_at_utc=datetime(2026, 5, 19, 21, 0, 30, tzinfo=UTC),
        successful_markets=(
            MarketSyncResult(
                market="TLT",
                success=True,
                bars_written=250,
                last_session_date=date(2026, 5, 19),
                front_month_expiry=None,
                error=None,
                open_interest=None,
            ),
        ),
        failed_markets=(
            MarketSyncResult(
                market="/MES",
                success=False,
                bars_written=0,
                last_session_date=None,
                front_month_expiry=None,
                error="fetch_futures_bars_and_front_month: no live front-month",
                open_interest=None,
            ),
        ),
    )


def _wire(api_app: FastAPI, source: object | None) -> None:
    api_app.dependency_overrides[_get_bar_sync_status_source] = lambda: source


# ---------------------------------------------------------------------------
# Provider unit tests
# ---------------------------------------------------------------------------


class TestBarSyncStatusProvider:
    def test_get_source_none_by_default(self) -> None:
        provider = BarSyncStatusProvider()
        assert provider.get_source() is None

    def test_set_then_get_source(self) -> None:
        provider = BarSyncStatusProvider()
        stub = _StubSource(last_cycle_result=None)
        provider.set_source(stub)
        assert provider.get_source() is stub

    def test_clear_drops_source(self) -> None:
        provider = BarSyncStatusProvider()
        provider.set_source(_StubSource(last_cycle_result=None))
        provider.clear()
        assert provider.get_source() is None

    def test_module_singleton_is_stable(self) -> None:
        assert get_bar_sync_status_provider() is get_bar_sync_status_provider()


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


class TestBarSyncStatusEndpointPending:
    @pytest.mark.asyncio
    async def test_no_source_returns_pending_not_running(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        _wire(api_app, None)
        resp = await api_client.get("/api/system/bar-sync")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["worker_running"] is False
        assert body["total_markets"] == 0
        assert body["successful_markets"] == []
        assert body["cycle_started_at_utc"] is None

    @pytest.mark.asyncio
    async def test_running_but_no_cycle_returns_pending_running(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        _wire(
            api_app,
            _StubSource(last_cycle_result=None, is_running=True),
        )
        resp = await api_client.get("/api/system/bar-sync")
        body = resp.json()
        assert body["status"] == "pending"
        assert body["worker_running"] is True


class TestBarSyncStatusEndpointOk:
    @pytest.mark.asyncio
    async def test_clean_cycle_returns_ok(self, api_client: AsyncClient, api_app: FastAPI) -> None:
        _wire(
            api_app,
            _StubSource(
                last_cycle_result=_ok_cycle(),
                last_fired_session_date_et=date(2026, 5, 19),
            ),
        )
        resp = await api_client.get("/api/system/bar-sync")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["successful_count"] == 2
        assert body["failed_count"] == 0
        assert body["total_markets"] == 2
        assert body["duration_seconds"] == 42.0
        assert body["last_fired_session_date_et"] == "2026-05-19"
        assert len(body["successful_markets"]) == 2
        mes = next(m for m in body["successful_markets"] if m["market"] == "/MES")
        assert mes["open_interest"] == 50000
        assert mes["front_month_expiry"] == "202606"


class TestBarSyncStatusEndpointDegraded:
    @pytest.mark.asyncio
    async def test_dirty_cycle_returns_degraded_with_failed_list(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        _wire(
            api_app,
            _StubSource(
                last_cycle_result=_dirty_cycle(),
                consecutive_failure_count=2,
            ),
        )
        resp = await api_client.get("/api/system/bar-sync")
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["failed_count"] == 1
        assert body["consecutive_failure_count"] == 2
        assert body["failed_markets"][0]["market"] == "/MES"
        assert "front-month" in body["failed_markets"][0]["error"]


class TestBarSyncStatusEndpointShape:
    @pytest.mark.asyncio
    async def test_response_keys_are_documented(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        _wire(api_app, _StubSource(last_cycle_result=_ok_cycle()))
        resp = await api_client.get("/api/system/bar-sync")
        body = resp.json()
        assert set(body.keys()) == {
            "status",
            "worker_running",
            "last_fired_session_date_et",
            "cycle_started_at_utc",
            "cycle_completed_at_utc",
            "duration_seconds",
            "successful_count",
            "failed_count",
            "total_markets",
            "consecutive_failure_count",
            "consecutive_sentinel_count",
            "successful_markets",
            "failed_markets",
            "server_now",
        }

    def test_schema_forbids_extra(self) -> None:
        from services.api.schemas.system import (
            BarSyncMarketResult,
            BarSyncStatusResponse,
        )

        assert BarSyncStatusResponse.model_config.get("extra") == "forbid"
        assert BarSyncMarketResult.model_config.get("extra") == "forbid"
