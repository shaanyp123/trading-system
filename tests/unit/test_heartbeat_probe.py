"""Unit tests for ``services.api.heartbeat_probe`` (PR #154 follow-up).

Covers:
* HeartbeatRegistry cooldown helpers (should_alert / mark_alerted / last_alerted_at)
* Per-tick stale detection (run_once) without/with alert hook
* Cooldown suppresses re-alerts; fresh heartbeat clears cooldown
* No stale → no audit + no alert
* Multiple services in one tick

Uses fake session_factory + monkeypatched append_audit_event to avoid
testcontainers. End-to-end audit + alerts row INSERT is covered by the
existing recon integration test pattern.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import services.api.heartbeat_probe as probe_mod
from services.api.heartbeat_probe import (
    HEARTBEAT_ALERT_CATEGORY,
    HEARTBEAT_ALERT_SEVERITY,
    HeartbeatProbe,
)
from services.api.heartbeats import (
    HEARTBEAT_ALERT_COOLDOWN_S,
    HEARTBEAT_STALE_THRESHOLDS_S,
    HeartbeatRegistry,
)


@asynccontextmanager
async def _async_null_cm() -> Any:
    yield None


def _fake_session_factory() -> Any:
    """No-op factory — probe doesn't read; only the audit writer does."""

    @asynccontextmanager
    async def _session_cm() -> Any:
        yield MagicMock()

    factory = MagicMock()
    factory.side_effect = lambda *a, **k: _session_cm()
    return factory


def _fake_audit_record(sequence_no: int = 20) -> Any:
    rec = MagicMock()
    rec.event_uuid = uuid4()
    rec.sequence_no = sequence_no
    return rec


# ---------------------------------------------------------------------------
# HeartbeatRegistry cooldown helpers
# ---------------------------------------------------------------------------


class TestHeartbeatRegistryCooldown:
    def test_should_alert_true_when_never_alerted(self) -> None:
        reg = HeartbeatRegistry()
        assert reg.should_alert("lean_cycle", now_utc=datetime(2026, 5, 16, tzinfo=UTC))

    def test_mark_alerted_sets_last_alerted(self) -> None:
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        reg.mark_alerted("lean_cycle", now_utc=now)
        assert reg.last_alerted_at("lean_cycle") == now

    def test_should_alert_false_during_cooldown(self) -> None:
        reg = HeartbeatRegistry()
        t0 = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        reg.mark_alerted("lean_cycle", now_utc=t0)
        # 1 hour after mark: still within 24h cooldown
        assert not reg.should_alert("lean_cycle", now_utc=t0 + timedelta(hours=1))
        # 23h 59min later: still within
        assert not reg.should_alert("lean_cycle", now_utc=t0 + timedelta(hours=23, minutes=59))

    def test_should_alert_true_after_cooldown(self) -> None:
        reg = HeartbeatRegistry()
        t0 = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        reg.mark_alerted("lean_cycle", now_utc=t0)
        # 24h + 1s past: cooldown expired
        assert reg.should_alert(
            "lean_cycle",
            now_utc=t0 + timedelta(seconds=HEARTBEAT_ALERT_COOLDOWN_S + 1),
        )

    @pytest.mark.asyncio
    async def test_record_clears_cooldown(self) -> None:
        """A fresh heartbeat clears the alert cooldown so the next
        staleness fires immediately rather than waiting 24h."""
        reg = HeartbeatRegistry()
        t0 = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        reg.mark_alerted("lean_cycle", now_utc=t0)
        await reg.record("lean_cycle", source_clock_utc=t0 + timedelta(hours=1))
        # Cooldown cleared
        assert reg.last_alerted_at("lean_cycle") is None
        assert reg.should_alert("lean_cycle", now_utc=t0 + timedelta(hours=2))

    def test_naive_now_raises_should_alert(self) -> None:
        reg = HeartbeatRegistry()
        with pytest.raises(ValueError, match="now_utc"):
            reg.should_alert("lean_cycle", now_utc=datetime(2026, 5, 16))

    def test_naive_now_raises_mark_alerted(self) -> None:
        reg = HeartbeatRegistry()
        with pytest.raises(ValueError, match="now_utc"):
            reg.mark_alerted("lean_cycle", now_utc=datetime(2026, 5, 16))

    def test_reset_clears_alerted_at(self) -> None:
        reg = HeartbeatRegistry()
        reg.mark_alerted("lean_cycle", now_utc=datetime(2026, 5, 16, tzinfo=UTC))
        reg.reset()
        assert reg.last_alerted_at("lean_cycle") is None


# ---------------------------------------------------------------------------
# HeartbeatProbe.run_once
# ---------------------------------------------------------------------------


class TestProbeRunOnce:
    @pytest.mark.asyncio
    async def test_no_records_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = HeartbeatRegistry()
        append = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(probe_mod, "append_audit_event", append)

        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
        )
        count = await probe.run_once(now_utc=datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
        assert count == 0
        append.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_record_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        await reg.record("lean_cycle", source_clock_utc=now - timedelta(hours=1))
        append = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(probe_mod, "append_audit_event", append)

        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
        )
        count = await probe.run_once(now_utc=now)
        assert count == 0
        append.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_record_emits_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        # Heartbeat 30h ago (past 26h threshold)
        await reg.record("lean_cycle", source_clock_utc=now - timedelta(hours=30))
        rec = _fake_audit_record(sequence_no=20)
        append = AsyncMock(return_value=rec)
        monkeypatch.setattr(probe_mod, "append_audit_event", append)

        account_id = uuid4()
        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=account_id,
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
        )
        count = await probe.run_once(now_utc=now)
        assert count == 1
        append.assert_awaited_once()

        # Audit event_type is HEARTBEAT_STALE_DETECTED
        from services.audit.event_types import AuditEventType

        assert append.await_args.args[1] == AuditEventType.HEARTBEAT_STALE_DETECTED
        # Payload carries service + thresholds
        payload = append.await_args.args[2]
        assert payload["service"] == "lean_cycle"
        assert payload["alert_severity"] == HEARTBEAT_ALERT_SEVERITY
        assert payload["alert_category"] == HEARTBEAT_ALERT_CATEGORY
        assert payload["stale_threshold_s"] == HEARTBEAT_STALE_THRESHOLDS_S["lean_cycle"]
        # source_clock_ts is now_utc
        assert append.await_args.kwargs["source_clock_ts"] == now
        # account_id threaded through
        assert append.await_args.kwargs["account_id"] == account_id

        # Mark-alerted side-effect
        assert reg.last_alerted_at("lean_cycle") == now

    @pytest.mark.asyncio
    async def test_stale_record_fires_alert_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        await reg.record("lean_cycle", source_clock_utc=now - timedelta(hours=30))
        monkeypatch.setattr(
            probe_mod, "append_audit_event", AsyncMock(return_value=_fake_audit_record())
        )

        hook = AsyncMock()
        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=hook,
        )
        await probe.run_once(now_utc=now)
        hook.assert_awaited_once()
        ctx = hook.await_args.args[0]
        # ctx.descriptor exposes the recon-compatible alert descriptor
        assert ctx.descriptor.title.startswith("Heartbeat stale: lean_cycle")
        assert ctx.descriptor.severity == HEARTBEAT_ALERT_SEVERITY
        assert ctx.descriptor.category == HEARTBEAT_ALERT_CATEGORY
        assert ctx.env == "paper"

    @pytest.mark.asyncio
    async def test_alert_hook_failure_does_not_suppress_audit_or_mark_alerted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hook failure logs WARNING but the audit row + mark_alerted both
        land — the alert is a notification, the audit is the durable trail."""
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        await reg.record("lean_cycle", source_clock_utc=now - timedelta(hours=30))
        monkeypatch.setattr(
            probe_mod, "append_audit_event", AsyncMock(return_value=_fake_audit_record())
        )

        hook = AsyncMock(side_effect=RuntimeError("discord 503"))
        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=hook,
        )
        count = await probe.run_once(now_utc=now)
        # Audit succeeded even though hook failed
        assert count == 1
        assert reg.last_alerted_at("lean_cycle") == now

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_re_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        await reg.record("lean_cycle", source_clock_utc=now - timedelta(hours=30))
        append = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(probe_mod, "append_audit_event", append)

        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
        )

        # First tick fires
        count1 = await probe.run_once(now_utc=now)
        assert count1 == 1

        # Second tick 1 hour later: cooldown active, no fire
        count2 = await probe.run_once(now_utc=now + timedelta(hours=1))
        assert count2 == 0
        # Only one audit write total
        assert append.await_count == 1

    @pytest.mark.asyncio
    async def test_fresh_heartbeat_clears_cooldown_for_next_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        await reg.record("lean_cycle", source_clock_utc=now - timedelta(hours=30))
        append = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(probe_mod, "append_audit_event", append)

        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
        )

        # First tick fires
        await probe.run_once(now_utc=now)

        # Recovery: fresh heartbeat clears cooldown
        await reg.record("lean_cycle", source_clock_utc=now + timedelta(minutes=10))

        # Failure again: cron stops 31h later
        later = now + timedelta(hours=32)
        # Mock the registry's record so the stale check sees the 30h-stale state
        # (we just stamped a heartbeat at now+10min; now+32h is 31h 50min later)
        count2 = await probe.run_once(now_utc=later)
        # Should fire again because cooldown was cleared by the fresh heartbeat
        assert count2 == 1
        assert append.await_count == 2

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_mark_alerted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the audit write fails, the cooldown is NOT stamped so the
        next tick retries. The audit chain is the source of truth — we
        must not silently swallow it."""
        reg = HeartbeatRegistry()
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        await reg.record("lean_cycle", source_clock_utc=now - timedelta(hours=30))
        # Audit append raises
        monkeypatch.setattr(
            probe_mod, "append_audit_event", AsyncMock(side_effect=RuntimeError("db down"))
        )

        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
        )
        count = await probe.run_once(now_utc=now)
        assert count == 0  # nothing emitted
        # Cooldown NOT stamped → next tick will retry
        assert reg.last_alerted_at("lean_cycle") is None

    def test_run_once_rejects_naive_now(self) -> None:
        reg = HeartbeatRegistry()
        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
        )
        import asyncio as _asyncio

        with pytest.raises(ValueError, match="now_utc"):
            _asyncio.run(probe.run_once(now_utc=datetime(2026, 5, 16, 12, 0)))


class TestProbeRunForever:
    @pytest.mark.asyncio
    async def test_request_stop_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = HeartbeatRegistry()
        monkeypatch.setattr(
            probe_mod, "append_audit_event", AsyncMock(return_value=_fake_audit_record())
        )
        probe = HeartbeatProbe(
            session_factory=_fake_session_factory(),
            account_id=uuid4(),
            env="paper",
            registry=reg,
            alert_dispatch_hook=None,
            interval_s=1,
        )
        import asyncio

        task = asyncio.create_task(probe.run_forever())
        # Let it run a tick
        await asyncio.sleep(0.05)
        probe.request_stop()
        await asyncio.wait_for(task, timeout=3.0)
        assert task.done()


class TestModuleContract:
    def test_exports(self) -> None:
        from services.api.heartbeat_probe import (
            HEARTBEAT_ALERT_CATEGORY,  # noqa: F401
            HEARTBEAT_ALERT_SEVERITY,  # noqa: F401
            HeartbeatAlertDescriptor,  # noqa: F401
            HeartbeatAlertDispatchHook,  # noqa: F401
            HeartbeatProbe,  # noqa: F401
        )

    def test_heartbeat_stale_in_audit_taxonomy(self) -> None:
        from services.audit.event_types import AuditEventType

        # The new locked value is in the enum
        assert AuditEventType.HEARTBEAT_STALE_DETECTED.value == "heartbeat_stale_detected"

    def test_heartbeat_stale_in_alert_category(self) -> None:
        from services.webhook_pusher.payloads import AlertCategory

        assert AlertCategory.HEARTBEAT_STALE.value == "heartbeat_stale"
