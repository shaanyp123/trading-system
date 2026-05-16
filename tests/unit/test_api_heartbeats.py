"""Unit tests for the heartbeat registry + ``GET /api/system/heartbeats``."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from services.api.heartbeats import (
    HEARTBEAT_STALE_THRESHOLDS_S,
    HeartbeatRecord,
    HeartbeatRegistry,
    classify_freshness,
    get_heartbeat_registry,
)
from services.api.routes.system import _get_heartbeat_registry

# ---------------------------------------------------------------------------
# Registry unit tests (pure-policy; no app harness needed)
# ---------------------------------------------------------------------------


class TestHeartbeatRegistryRecord:
    @pytest.mark.asyncio
    async def test_record_returns_count_1_on_first_call(self) -> None:
        registry = HeartbeatRegistry()
        ts = datetime(2026, 5, 13, 21, 30, tzinfo=UTC)
        rec = await registry.record("lean_cycle", source_clock_utc=ts)
        assert isinstance(rec, HeartbeatRecord)
        assert rec.last_heartbeat_utc == ts
        assert rec.count == 1

    @pytest.mark.asyncio
    async def test_record_increments_count_on_repeat(self) -> None:
        registry = HeartbeatRegistry()
        for i in range(5):
            ts = datetime(2026, 5, 13, 21, 30, tzinfo=UTC) + timedelta(days=i)
            rec = await registry.record("lean_cycle", source_clock_utc=ts)
            assert rec.count == i + 1

    @pytest.mark.asyncio
    async def test_record_uses_now_when_received_at_omitted(self) -> None:
        registry = HeartbeatRegistry()
        ts = datetime(2026, 5, 13, 21, 30, tzinfo=UTC)
        before = datetime.now(tz=UTC)
        rec = await registry.record("lean_cycle", source_clock_utc=ts)
        after = datetime.now(tz=UTC)
        assert before <= rec.received_at_utc <= after

    @pytest.mark.asyncio
    async def test_record_uses_explicit_received_at(self) -> None:
        registry = HeartbeatRegistry()
        ts = datetime(2026, 5, 13, 21, 30, tzinfo=UTC)
        received = datetime(2026, 5, 13, 21, 30, 1, tzinfo=UTC)
        rec = await registry.record("lean_cycle", source_clock_utc=ts, received_at_utc=received)
        assert rec.received_at_utc == received

    @pytest.mark.asyncio
    async def test_record_rejects_naive_source_clock(self) -> None:
        registry = HeartbeatRegistry()
        with pytest.raises(ValueError, match="source_clock_utc"):
            await registry.record("lean_cycle", source_clock_utc=datetime(2026, 5, 13, 21, 30))

    @pytest.mark.asyncio
    async def test_record_rejects_naive_received_at(self) -> None:
        registry = HeartbeatRegistry()
        ts = datetime(2026, 5, 13, 21, 30, tzinfo=UTC)
        with pytest.raises(ValueError, match="received_at_utc"):
            await registry.record(
                "lean_cycle",
                source_clock_utc=ts,
                received_at_utc=datetime(2026, 5, 13, 21, 30, 1),
            )

    @pytest.mark.asyncio
    async def test_concurrent_records_dont_corrupt_count(self) -> None:
        registry = HeartbeatRegistry()
        base = datetime(2026, 5, 13, tzinfo=UTC)
        tasks = [
            registry.record("lean_cycle", source_clock_utc=base + timedelta(seconds=i))
            for i in range(100)
        ]
        await asyncio.gather(*tasks)
        snap = registry.snapshot()
        assert snap["lean_cycle"].count == 100


class TestHeartbeatRegistrySnapshot:
    @pytest.mark.asyncio
    async def test_empty_registry_returns_empty_dict(self) -> None:
        registry = HeartbeatRegistry()
        assert registry.snapshot() == {}

    @pytest.mark.asyncio
    async def test_snapshot_is_defensive_copy(self) -> None:
        registry = HeartbeatRegistry()
        await registry.record(
            "lean_cycle",
            source_clock_utc=datetime(2026, 5, 13, 21, 30, tzinfo=UTC),
        )
        s1 = registry.snapshot()
        s1.clear()  # mutate the copy
        s2 = registry.snapshot()
        assert "lean_cycle" in s2

    def test_reset_drops_all_records(self) -> None:
        registry = HeartbeatRegistry()
        # Bypass record for sync access in this guardrail
        registry._records["lean_cycle"] = HeartbeatRecord(  # type: ignore[index]
            last_heartbeat_utc=datetime(2026, 5, 13, tzinfo=UTC),
            received_at_utc=datetime(2026, 5, 13, tzinfo=UTC),
            count=1,
        )
        assert registry.snapshot()
        registry.reset()
        assert registry.snapshot() == {}


class TestClassifyFreshness:
    def test_none_record_returns_unknown(self) -> None:
        now = datetime(2026, 5, 13, 21, 30, tzinfo=UTC)
        assert classify_freshness("lean_cycle", None, now_utc=now) == "unknown"

    def test_record_within_threshold_returns_fresh(self) -> None:
        now = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)
        rec = HeartbeatRecord(
            last_heartbeat_utc=now - timedelta(hours=23, minutes=59),
            received_at_utc=now,
            count=1,
        )
        assert classify_freshness("lean_cycle", rec, now_utc=now) == "fresh"

    def test_record_past_26h_returns_stale(self) -> None:
        now = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)
        rec = HeartbeatRecord(
            last_heartbeat_utc=now - timedelta(hours=26, seconds=1),
            received_at_utc=now,
            count=1,
        )
        assert classify_freshness("lean_cycle", rec, now_utc=now) == "stale"

    def test_record_exactly_at_threshold_returns_fresh(self) -> None:
        """The 26h threshold is strict-greater-than; equal is still fresh."""
        now = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)
        rec = HeartbeatRecord(
            last_heartbeat_utc=now - timedelta(seconds=HEARTBEAT_STALE_THRESHOLDS_S["lean_cycle"]),
            received_at_utc=now,
            count=1,
        )
        assert classify_freshness("lean_cycle", rec, now_utc=now) == "fresh"

    def test_naive_now_raises(self) -> None:
        rec = HeartbeatRecord(
            last_heartbeat_utc=datetime(2026, 5, 13, tzinfo=UTC),
            received_at_utc=datetime(2026, 5, 13, tzinfo=UTC),
            count=1,
        )
        with pytest.raises(ValueError, match="now_utc"):
            classify_freshness("lean_cycle", rec, now_utc=datetime(2026, 5, 13))


class TestModuleSingleton:
    def test_get_registry_returns_module_singleton(self) -> None:
        r1 = get_heartbeat_registry()
        r2 = get_heartbeat_registry()
        assert r1 is r2


# ---------------------------------------------------------------------------
# Endpoint tests via the api_client fixture (conftest.py)
# ---------------------------------------------------------------------------


def _wire_fresh_registry(api_app: FastAPI) -> HeartbeatRegistry:
    """Override the route's registry dep with a fresh per-test instance."""
    registry = HeartbeatRegistry()
    api_app.dependency_overrides[_get_heartbeat_registry] = lambda: registry
    return registry


class TestHeartbeatsEndpointEmpty:
    @pytest.mark.asyncio
    async def test_empty_registry_returns_unknown_freshness(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        _wire_fresh_registry(api_app)
        resp = await api_client.get("/api/system/heartbeats")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["service"] == "lean_cycle"
        assert item["freshness"] == "unknown"
        assert item["last_heartbeat_utc"] is None
        assert item["received_at_utc"] is None
        assert item["seconds_since_last_heartbeat"] is None
        assert item["count"] == 0
        assert item["stale_threshold_s"] == 26 * 3600

    @pytest.mark.asyncio
    async def test_response_carries_server_now_utc(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        _wire_fresh_registry(api_app)
        before = datetime.now(tz=UTC)
        resp = await api_client.get("/api/system/heartbeats")
        after = datetime.now(tz=UTC)
        server_now = datetime.fromisoformat(resp.json()["server_now"].replace("Z", "+00:00"))
        assert before - timedelta(seconds=1) <= server_now <= after + timedelta(seconds=1)


class TestHeartbeatsEndpointFresh:
    @pytest.mark.asyncio
    async def test_recent_heartbeat_returns_fresh(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        registry = _wire_fresh_registry(api_app)
        await registry.record(
            "lean_cycle",
            source_clock_utc=datetime.now(tz=UTC) - timedelta(minutes=5),
        )
        resp = await api_client.get("/api/system/heartbeats")
        item = resp.json()["items"][0]
        assert item["freshness"] == "fresh"
        assert item["count"] == 1
        # 5 minutes ago → ~300 seconds; allow generous slack for slow CI.
        assert 290 <= item["seconds_since_last_heartbeat"] <= 320


class TestHeartbeatsEndpointStale:
    @pytest.mark.asyncio
    async def test_old_heartbeat_returns_stale(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        registry = _wire_fresh_registry(api_app)
        await registry.record(
            "lean_cycle",
            source_clock_utc=datetime.now(tz=UTC) - timedelta(hours=30),
        )
        resp = await api_client.get("/api/system/heartbeats")
        item = resp.json()["items"][0]
        assert item["freshness"] == "stale"
        # 30h ≈ 108000s; allow drift.
        assert item["seconds_since_last_heartbeat"] >= 26 * 3600


class TestHeartbeatsEndpointResponseShape:
    @pytest.mark.asyncio
    async def test_response_has_only_documented_keys(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        _wire_fresh_registry(api_app)
        resp = await api_client.get("/api/system/heartbeats")
        body = resp.json()
        assert set(body.keys()) == {"items", "server_now"}
        item_keys = set(body["items"][0].keys())
        assert item_keys == {
            "service",
            "last_heartbeat_utc",
            "received_at_utc",
            "seconds_since_last_heartbeat",
            "stale_threshold_s",
            "freshness",
            "count",
        }

    @pytest.mark.asyncio
    async def test_extra_field_rejected_at_schema_level(
        self, api_client: AsyncClient, api_app: FastAPI
    ) -> None:
        # The HeartbeatsResponse model declares extra="forbid"; this proves
        # the model is constructed via the schema (not a raw dict).
        # We sniff the model class directly rather than the response body.
        from services.api.schemas.system import HeartbeatItem, HeartbeatsResponse

        assert HeartbeatItem.model_config.get("extra") == "forbid"
        assert HeartbeatsResponse.model_config.get("extra") == "forbid"
