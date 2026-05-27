"""Unit tests for ``services/qc_adapter/signal_ingestion.py``.

Test inventory:

- TestPayloadValidation — wrong event_type, missing keys, bad direction,
  bad market, bad target_contracts → SignalIngestError
- TestDecimalCoercion — JSON float decision_price preserved via str()
  (A05 enforcement); negative + zero + integer-shaped accepted
- TestSentinelDerivation — strategy_hash 40 hex chars; parameter_set_hash
  64 hex chars; slippage_calibration_version_id falls back to zero UUID
  when no head row; uses head row when present
- TestSessionDate — ET anchor; daylight-saving day; non-ET timestamp
- TestStrategyHashExtraction — version suffix already 40-char hex →
  use as-is; non-hex → SHA-1 the whole version string
- TestHappyPath — audit append called first (mocked) + INSERT row
  carries Decimal decision_price + dict sizing_trace; returns
  IngestResult with both UUIDs

A22 N/A — fake AsyncSession, no audit DB writes, no testcontainers.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from services.audit.event_types import AuditEventType
from services.audit.models import AuditLogRecord
from services.qc_adapter import signal_ingestion as si
from services.qc_adapter.payloads import QCEvent
from services.qc_adapter.signal_ingestion import (
    IngestResult,
    SignalIngestError,
    ingest_signal_emitted,
)

_ACCOUNT = UUID("019e1234-0000-7000-8000-000000000001")
_BASE_TS = datetime(2026, 5, 14, 21, 30, 0, tzinfo=UTC)  # 17:30 ET on Thursday


def _make_event(
    *,
    event_type: str = "signal_emitted",
    payload: dict[str, Any] | None = None,
    qc_algorithm_version: str = "v1-trend-following-9d2f7a1c20bcdef0123456789abcdef012345678",
    sequence_no: int = 42,
    ts: datetime | None = None,
) -> QCEvent:
    if payload is None:
        payload = {
            "market": "MES",
            "direction": "long",
            "target_contracts": 1,
            "decision_price": 5234.50,
            "sizing_trace": {"stage_0": "passed"},
        }
    return QCEvent(
        sequence_no=sequence_no,
        event_type=event_type,
        source_clock_ts=ts or _BASE_TS,
        qc_algorithm_version=qc_algorithm_version,
        payload=payload,
    )


@pytest.fixture
def fake_audit_append(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace append_audit_event with an AsyncMock returning a record."""
    mock = AsyncMock()
    audit_event_uuid = UUID("019e5678-0000-7000-8000-000000000002")
    mock.return_value = AuditLogRecord(
        sequence_no=10,
        event_uuid=audit_event_uuid,
        event_type=AuditEventType.SIGNAL_EMITTED,
        ingest_clock_ts=datetime.now(tz=UTC),
    )
    monkeypatch.setattr(si, "append_audit_event", mock)
    return mock


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Replace db.fetch_slippage_head_version_id, insert_slippage_bootstrap_row,
    and insert_signal_row.

    Default: slippage head returns a fixed UUID (no bootstrap needed). Tests
    that exercise the bootstrap path override slippage to None and assert
    bootstrap was called.
    """
    head_uuid = UUID("019eaaaa-0000-7000-8000-000000000004")
    slip = AsyncMock(return_value=head_uuid)
    bootstrap = AsyncMock(return_value=head_uuid)
    insert = AsyncMock(return_value=UUID("019e9999-0000-7000-8000-000000000003"))
    monkeypatch.setattr(si.qc_db, "fetch_slippage_head_version_id", slip)
    monkeypatch.setattr(si.qc_db, "insert_slippage_bootstrap_row", bootstrap)
    monkeypatch.setattr(si.qc_db, "insert_signal_row", insert)
    return {
        "slippage": slip,
        "bootstrap": bootstrap,
        "insert": insert,
        "head_uuid": head_uuid,
    }


@pytest.fixture
def fake_session_factory() -> MagicMock:
    """Session-factory stub returning async-context-manager-friendly sessions.

    The signal_ingestion module calls ``async with session_factory() as s:``
    multiple times; the factory is invoked SYNCHRONOUSLY (it's a class
    constructor, not a coroutine), and the returned session is the async
    context manager. So the factory is a MagicMock, not an AsyncMock.
    """

    def _build_session() -> MagicMock:
        s = MagicMock()
        s.__aenter__ = AsyncMock(return_value=s)
        s.__aexit__ = AsyncMock(return_value=None)
        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=s)
        begin.__aexit__ = AsyncMock(return_value=None)
        s.begin = lambda: begin
        return s

    factory = MagicMock(side_effect=_build_session)
    return factory


# ---------------------------------------------------------------------------
# TestPayloadValidation
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    @pytest.mark.asyncio
    async def test_wrong_event_type_raises(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(event_type="order_filled")
        with pytest.raises(SignalIngestError, match="event_type"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )
        fake_audit_append.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing_key",
        ["market", "direction", "target_contracts", "decision_price", "sizing_trace"],
    )
    async def test_missing_required_key_raises(
        self,
        missing_key: str,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        payload = {
            "market": "MES",
            "direction": "long",
            "target_contracts": 1,
            "decision_price": 5234.50,
            "sizing_trace": {},
        }
        del payload[missing_key]
        event = _make_event(payload=payload)
        with pytest.raises(SignalIngestError, match="missing"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )
        fake_audit_append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bad_direction_raises(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "sideways",
                "target_contracts": 1,
                "decision_price": 5234.50,
                "sizing_trace": {},
            }
        )
        with pytest.raises(SignalIngestError, match="direction"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_empty_market_raises(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": 5234.50,
                "sizing_trace": {},
            }
        )
        with pytest.raises(SignalIngestError, match="market"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_target_contracts_float_raises(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "long",
                "target_contracts": 1.5,  # float, not int
                "decision_price": 5234.50,
                "sizing_trace": {},
            }
        )
        with pytest.raises(SignalIngestError, match="target_contracts"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_target_contracts_bool_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "long",
                "target_contracts": True,  # bool is int subclass — reject
                "decision_price": 5234.50,
                "sizing_trace": {},
            }
        )
        with pytest.raises(SignalIngestError, match="target_contracts"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )


# ---------------------------------------------------------------------------
# TestDecimalCoercion
# ---------------------------------------------------------------------------


class TestDecimalCoercion:
    @pytest.mark.asyncio
    async def test_float_decision_price_preserved_via_str(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": 5234.50,
                "sizing_trace": {},
            }
        )
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        # The audit payload's decision_price is a Decimal.
        audit_payload = fake_audit_append.await_args.args[2]
        assert audit_payload["decision_price"] == Decimal("5234.5")
        # And the signals INSERT received a Decimal.
        insert_row = fake_db["insert"].await_args.args[1]
        assert insert_row.decision_price == Decimal("5234.5")

    @pytest.mark.asyncio
    async def test_integer_decision_price_accepted(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": 5234,  # int
                "sizing_trace": {},
            }
        )
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        insert_row = fake_db["insert"].await_args.args[1]
        assert insert_row.decision_price == Decimal("5234")


# ---------------------------------------------------------------------------
# TestSentinelDerivation
# ---------------------------------------------------------------------------


class TestSentinelDerivation:
    @pytest.mark.asyncio
    async def test_strategy_hash_is_40_hex_chars(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event()
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        insert_row = fake_db["insert"].await_args.args[1]
        assert len(insert_row.strategy_hash) == 40
        assert all(c in "0123456789abcdef" for c in insert_row.strategy_hash)

    @pytest.mark.asyncio
    async def test_parameter_set_hash_is_64_hex_chars(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event()
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        insert_row = fake_db["insert"].await_args.args[1]
        assert len(insert_row.parameter_set_hash) == 64
        assert all(c in "0123456789abcdef" for c in insert_row.parameter_set_hash)

    @pytest.mark.asyncio
    async def test_slippage_bootstraps_when_no_head(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        bootstrap_uuid = UUID("019ebbbb-0000-7000-8000-000000000005")
        fake_db["slippage"].return_value = None
        fake_db["bootstrap"].return_value = bootstrap_uuid

        event = _make_event()
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        # Bootstrap was called exactly once
        fake_db["bootstrap"].assert_awaited_once()
        # And the bootstrap UUID flows into the signals INSERT
        insert_row = fake_db["insert"].await_args.args[1]
        assert insert_row.slippage_calibration_version_id == bootstrap_uuid

    @pytest.mark.asyncio
    async def test_slippage_uses_head_uuid_when_present(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        # fake_db default returns a head UUID; bootstrap not invoked.
        event = _make_event()
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        insert_row = fake_db["insert"].await_args.args[1]
        assert insert_row.slippage_calibration_version_id == fake_db["head_uuid"]
        fake_db["bootstrap"].assert_not_awaited()


# ---------------------------------------------------------------------------
# TestStrategyHashExtraction (pure helpers)
# ---------------------------------------------------------------------------


class TestStrategyHashExtraction:
    def test_40_hex_suffix_extracted_as_is(self) -> None:
        version = "v1-trend-following-abcdef0123456789abcdef0123456789abcdef01"
        result = si._derive_strategy_hash(version)
        assert result == "abcdef0123456789abcdef0123456789abcdef01"

    def test_non_hex_suffix_sha1s_whole_string(self) -> None:
        version = "v1-trend-following-experimental-build"
        expected = hashlib.sha1(version.encode("utf-8")).hexdigest()
        result = si._derive_strategy_hash(version)
        assert result == expected
        assert len(result) == 40


# ---------------------------------------------------------------------------
# TestSessionDate
# ---------------------------------------------------------------------------


class TestSessionDate:
    def test_session_date_anchored_on_et_wall_clock(self) -> None:
        # 17:30 ET on Thursday May 14 2026 = 21:30 UTC same day (during DST)
        ts = datetime(2026, 5, 14, 21, 30, 0, tzinfo=UTC)
        result = si._session_date_from_utc(ts)
        # In ET that's 17:30 on May 14 → session_date = 2026-05-14
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 14

    def test_pre_utc_midnight_et_is_prior_calendar_day(self) -> None:
        # 03:00 UTC on May 15 = 23:00 ET on May 14 → session_date = May 14
        ts = datetime(2026, 5, 15, 3, 0, 0, tzinfo=UTC)
        result = si._session_date_from_utc(ts)
        assert result.day == 14
        assert result.month == 5


# ---------------------------------------------------------------------------
# TestHappyPath
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_ingest_result_with_both_uuids(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event()
        result = await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        assert isinstance(result, IngestResult)
        # audit_event_uuid matches the mocked return
        assert result.audit_event_uuid == UUID("019e5678-0000-7000-8000-000000000002")
        # signal_id matches the insert_signal_row mock return
        assert result.signal_id == UUID("019e9999-0000-7000-8000-000000000003")

    @pytest.mark.asyncio
    async def test_audit_called_before_insert(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        """Audit-first ordering per backend-spec §2.10.1."""
        call_order: list[str] = []

        async def audit_side(*args: Any, **kwargs: Any) -> AuditLogRecord:
            call_order.append("audit")
            return AuditLogRecord(
                sequence_no=10,
                event_uuid=UUID("019e5678-0000-7000-8000-000000000002"),
                event_type=AuditEventType.SIGNAL_EMITTED,
                ingest_clock_ts=datetime.now(tz=UTC),
            )

        async def insert_side(*args: Any, **kwargs: Any) -> UUID:
            call_order.append("insert")
            return UUID("019e9999-0000-7000-8000-000000000003")

        fake_audit_append.side_effect = audit_side
        fake_db["insert"].side_effect = insert_side

        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=_make_event(),
        )
        assert call_order == ["audit", "insert"]

    @pytest.mark.asyncio
    async def test_signal_type_defaults_to_entry(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        """PR-B (2026-05-26): payload without signal_type falls back to 'entry'
        for backwards compat with pre-PR-B emitters."""
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=_make_event(),
        )
        insert_row = fake_db["insert"].await_args.args[1]
        assert insert_row.signal_type == "entry"

    @pytest.mark.asyncio
    async def test_signal_type_explicit_entry_round_trips(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": 5234.50,
                "sizing_trace": {"stage_0": "passed"},
                "signal_type": "entry",
            }
        )
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        insert_row = fake_db["insert"].await_args.args[1]
        assert insert_row.signal_type == "entry"
        audit_payload = fake_audit_append.await_args.args[2]
        assert audit_payload["signal_type"] == "entry"

    @pytest.mark.asyncio
    async def test_audit_payload_has_decimal_decision_price(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        """A05 — JCS canonicalization rejects floats."""
        from services.audit.chain import jcs_serialize

        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=_make_event(),
        )
        audit_payload = fake_audit_append.await_args.args[2]
        # If any float remained, jcs_serialize raises TypeError per A05.
        canonical = jcs_serialize(audit_payload)
        assert isinstance(canonical, bytes)


# ---------------------------------------------------------------------------
# TestExitSignalIngestion (PR-B, 2026-05-26)
# ---------------------------------------------------------------------------


def _make_exit_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "market": "MES",
        "direction": "flat",
        "target_contracts": 0,
        "decision_price": 5234.50,
        "sizing_trace": {"schema_version": 1},
        "signal_type": "exit",
        "exit_reason": "trend_flip",
        "prior_position_direction": "long",
        "prior_position_quantity": 3,
    }
    payload.update(overrides)
    return payload


class TestExitSignalIngestion:
    """Exit-path discriminator + companion-field validation per design §Q2."""

    @pytest.mark.asyncio
    async def test_exit_payload_lands_with_signal_type_exit(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(payload=_make_exit_payload())
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        insert_row = fake_db["insert"].await_args.args[1]
        assert insert_row.signal_type == "exit"
        assert insert_row.direction == "flat"
        audit_payload = fake_audit_append.await_args.args[2]
        assert audit_payload["signal_type"] == "exit"
        assert audit_payload["exit_reason"] == "trend_flip"
        assert audit_payload["prior_position_direction"] == "long"
        assert audit_payload["prior_position_quantity"] == 3
        # paired_entry_market not set for trend_flip
        assert "paired_entry_market" not in audit_payload

    @pytest.mark.asyncio
    async def test_reversal_exit_with_paired_entry_market(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload=_make_exit_payload(
                exit_reason="reversal",
                paired_entry_market="MES",
            )
        )
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        audit_payload = fake_audit_append.await_args.args[2]
        assert audit_payload["exit_reason"] == "reversal"
        assert audit_payload["paired_entry_market"] == "MES"

    @pytest.mark.asyncio
    async def test_decommission_exit_short_position(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload=_make_exit_payload(
                exit_reason="decommission",
                prior_position_direction="short",
                prior_position_quantity=-5,
            )
        )
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        audit_payload = fake_audit_append.await_args.args[2]
        assert audit_payload["exit_reason"] == "decommission"
        assert audit_payload["prior_position_direction"] == "short"
        assert audit_payload["prior_position_quantity"] == -5

    @pytest.mark.asyncio
    async def test_invalid_signal_type_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": 5234.50,
                "sizing_trace": {},
                "signal_type": "bogus",
            }
        )
        with pytest.raises(SignalIngestError, match="signal_type"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )
        fake_audit_append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exit_missing_exit_reason_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        payload = _make_exit_payload()
        del payload["exit_reason"]
        event = _make_event(payload=payload)
        with pytest.raises(SignalIngestError, match="exit_reason"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )
        fake_audit_append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exit_invalid_exit_reason_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(payload=_make_exit_payload(exit_reason="bogus"))
        with pytest.raises(SignalIngestError, match="exit_reason"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_exit_missing_prior_position_direction_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        payload = _make_exit_payload()
        del payload["prior_position_direction"]
        event = _make_event(payload=payload)
        with pytest.raises(SignalIngestError, match="prior_position_direction"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_exit_prior_position_direction_flat_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        """Exits against a FLAT prior position are nonsensical — reject."""
        event = _make_event(payload=_make_exit_payload(prior_position_direction="flat"))
        with pytest.raises(SignalIngestError, match="prior_position_direction"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_exit_missing_prior_position_quantity_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        payload = _make_exit_payload()
        del payload["prior_position_quantity"]
        event = _make_event(payload=payload)
        with pytest.raises(SignalIngestError, match="prior_position_quantity"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_exit_prior_position_quantity_zero_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        event = _make_event(payload=_make_exit_payload(prior_position_quantity=0))
        with pytest.raises(SignalIngestError, match="prior_position_quantity"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_exit_paired_entry_on_trend_flip_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        """paired_entry_market is reversal-only per design §Q1."""
        event = _make_event(payload=_make_exit_payload(paired_entry_market="MES"))
        with pytest.raises(SignalIngestError, match="reversal"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_entry_payload_with_exit_field_rejected(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        """Entry payloads must not carry exit_reason / prior_position_*. Defensive
        against emitter bugs that copy fields between paths."""
        event = _make_event(
            payload={
                "market": "MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": 5234.50,
                "sizing_trace": {},
                "signal_type": "entry",
                "exit_reason": "trend_flip",
            }
        )
        with pytest.raises(SignalIngestError, match="exit_reason"):
            await ingest_signal_emitted(
                fake_session_factory,
                account_id=_ACCOUNT,
                env="paper",
                phase_at_emit=0,
                event=event,
            )

    @pytest.mark.asyncio
    async def test_exit_audit_payload_jcs_serializable(
        self,
        fake_audit_append: AsyncMock,
        fake_db: dict[str, AsyncMock],
        fake_session_factory: MagicMock,
    ) -> None:
        """Exit audit payload must satisfy A05 — JCS rejects floats."""
        from services.audit.chain import jcs_serialize

        event = _make_event(payload=_make_exit_payload())
        await ingest_signal_emitted(
            fake_session_factory,
            account_id=_ACCOUNT,
            env="paper",
            phase_at_emit=0,
            event=event,
        )
        audit_payload = fake_audit_append.await_args.args[2]
        canonical = jcs_serialize(audit_payload)
        assert isinstance(canonical, bytes)
