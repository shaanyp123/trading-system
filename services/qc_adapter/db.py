"""services/qc_adapter/db.py — DB query layer.

Owns the SQL the orchestrator + signal_ingestion modules issue against
Postgres. Mirrors the ``Phase1QueryRepo`` pattern from
``services/api/repos/phase1.py`` but lives inside the qc_adapter
service so PR-A doesn't touch the api package.

Roles + grants (alembic 0006):
  * Uses ``app_service`` Postgres role.
  * SELECT + INSERT + UPDATE on ``qc_adapter_cursor``.
  * SELECT + INSERT on ``signals`` + ``data_quality_events``.
  * SELECT on ``accounts`` + ``slippage_calibration_versions``.

All datetime values are tz-aware UTC (A06). Decimal-from-JSON-number
conversion happens at the ``signal_ingestion`` boundary, NOT here — this
module deals only in already-typed values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .cursor import CursorAdvance, CursorDirectory, CursorRow


@dataclass(frozen=True, slots=True)
class SignalInsertRow:
    """Typed values for a single signals INSERT.

    Order matches the column list in the SQL below; mypy + Pydantic
    cannot enforce the schema FK or CHECK constraints, so the audit
    writer's JCS pre-validation is the canonical type guard.
    """

    account_id: UUID
    env: str
    market: str
    emitted_at_utc: datetime
    session_date: datetime  # interpreted as DATE at the SQL boundary
    direction: str
    signal_type: str
    strategy_hash: str
    parameter_set_hash: str
    slippage_calibration_version_id: UUID
    decision_price: Decimal
    target_contracts: int
    sizing_trace: dict[str, Any]


async def fetch_active_account_id(session: AsyncSession) -> UUID | None:
    """Mirror of ``services.api.repos.phase1.fetch_active_account_id``.

    Returns the single live account row's id, or None when the accounts
    table is empty (a freshly-migrated DB before the operator bootstrap
    flow).
    """
    row = (
        await session.execute(
            text("SELECT id FROM accounts WHERE active_to IS NULL ORDER BY active_from ASC LIMIT 1")
        )
    ).fetchone()
    return row.id if row else None


async def fetch_cursor_row(session: AsyncSession, directory: CursorDirectory) -> CursorRow:
    """Read the current ``qc_adapter_cursor`` row for ``directory``.

    The three canonical rows are seeded by alembic 0004 — a missing row
    indicates schema drift and we raise loudly.
    """
    row = (
        await session.execute(
            text(
                "SELECT directory_path, last_consumed_sequence_no, "
                "       last_consumed_at_utc, bytes_consumed, "
                "       consecutive_failures "
                "FROM qc_adapter_cursor "
                "WHERE directory_path = :path"
            ),
            {"path": directory.value},
        )
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"qc_adapter_cursor row missing for {directory.value!r} — "
            "alembic 0004 should have seeded all three canonical "
            "directories per backend-spec §3.19"
        )
    return CursorRow(
        directory_path=directory,
        last_consumed_sequence_no=row.last_consumed_sequence_no,
        last_consumed_at_utc=row.last_consumed_at_utc,
        bytes_consumed=row.bytes_consumed,
        consecutive_failures=row.consecutive_failures,
    )


async def apply_cursor_advance(session: AsyncSession, advance: CursorAdvance) -> None:
    """UPDATE the cursor row with the post-state from a poll cycle.

    Idempotent under retry because :class:`CursorAdvance` carries the
    full post-state (not a diff).
    """
    await session.execute(
        text(
            "UPDATE qc_adapter_cursor "
            "SET last_consumed_sequence_no = :seq, "
            "    last_consumed_at_utc = :ts, "
            "    bytes_consumed = :bytes, "
            "    consecutive_failures = :failures "
            "WHERE directory_path = :path"
        ),
        {
            "seq": advance.last_consumed_sequence_no,
            "ts": advance.last_consumed_at_utc,
            "bytes": advance.bytes_consumed,
            "failures": advance.consecutive_failures,
            "path": advance.directory_path.value,
        },
    )


async def fetch_slippage_head_version_id(session: AsyncSession) -> UUID | None:
    """Look up the active HEAD row in ``slippage_calibration_versions``.

    Backend-spec §2.14 says Phase 0 / Phase 1 first 30 days has no
    calibration row yet (zero-prior bootstrap); returning None tells the
    caller to bootstrap via :func:`insert_slippage_bootstrap_row`.
    """
    row = (
        await session.execute(
            text("SELECT id FROM slippage_calibration_versions WHERE is_head = TRUE LIMIT 1")
        )
    ).fetchone()
    return UUID(str(row.id)) if row else None


async def insert_slippage_bootstrap_row(
    session: AsyncSession,
    *,
    audit_event_uuid: UUID,
    calibrated_at_utc: datetime,
) -> UUID:
    """Insert the Phase-0 zero-prior bootstrap row in
    ``slippage_calibration_versions`` and return its UUID.

    Per backend-spec §2.14: ``trigger='bootstrap'``, alpha=0/beta=0 across
    all markets, ``is_head=TRUE``. Caller MUST short-circuit if
    :func:`fetch_slippage_head_version_id` already returned non-None
    (the ``slippage_head`` partial unique index in alembic 0003 would
    raise IntegrityError on a second concurrent insert).
    """
    inserted = (
        await session.execute(
            text(
                "INSERT INTO slippage_calibration_versions ("
                "  version_no, is_head, calibrated_at_utc, trigger, "
                "  per_market_coefficients, data_window_start, data_window_end, "
                "  audit_event_uuid, notes"
                ") VALUES ("
                "  1, TRUE, :ts, 'bootstrap', "
                "  CAST(:coeffs AS jsonb), NULL, NULL, "
                "  :audit_uuid, :notes"
                ") RETURNING id"
            ),
            {
                "ts": calibrated_at_utc,
                "coeffs": _to_canonical_json_string({"_bootstrap": "zero-prior"}),
                "audit_uuid": audit_event_uuid,
                "notes": (
                    "Phase-0 zero-prior bootstrap per backend-spec §2.14. "
                    "Replaced by first real OLS fit after 30 days of live fills."
                ),
            },
        )
    ).one()
    return UUID(str(inserted.id))


async def insert_signal_row(session: AsyncSession, row: SignalInsertRow) -> UUID:
    """Insert one row into ``signals`` and return its UUID.

    Status defaults to 'pending' per the alembic 0002 CHECK constraint;
    the signal-mutation handlers (PR-B) flip it to approved/rejected/
    deferred when the operator acts.
    """
    inserted = (
        await session.execute(
            text(
                "INSERT INTO signals ("
                "    account_id, env, market, "
                "    emitted_at_utc, session_date, direction, signal_type, "
                "    strategy_hash, parameter_set_hash, slippage_calibration_version_id, "
                "    decision_price, target_contracts, sizing_trace, status"
                ") VALUES ("
                "    :account_id, :env, :market, "
                "    :emitted_at_utc, :session_date, :direction, :signal_type, "
                "    :strategy_hash, :parameter_set_hash, :slippage_calibration_version_id, "
                "    :decision_price, :target_contracts, "
                "    CAST(:sizing_trace AS jsonb), 'pending'"
                ") RETURNING id"
            ),
            {
                "account_id": row.account_id,
                "env": row.env,
                "market": row.market,
                "emitted_at_utc": row.emitted_at_utc,
                "session_date": row.session_date,
                "direction": row.direction,
                "signal_type": row.signal_type,
                "strategy_hash": row.strategy_hash,
                "parameter_set_hash": row.parameter_set_hash,
                "slippage_calibration_version_id": row.slippage_calibration_version_id,
                "decision_price": row.decision_price,
                "target_contracts": row.target_contracts,
                "sizing_trace": _to_canonical_json_string(row.sizing_trace),
            },
        )
    ).one()
    return UUID(str(inserted.id))


async def insert_data_quality_event(
    session: AsyncSession,
    *,
    market: str,
    reason: str,
    raw_record: dict[str, Any] | str,
    audit_event_uuid: UUID,
    event_type: str = "quarantine",
    detected_at_utc: datetime | None = None,
) -> UUID:
    """Insert a row into ``data_quality_events``.

    Used to quarantine malformed QC records so a single bad line doesn't
    poison the batch. ``audit_event_uuid`` MUST point at the
    ``DATA_QUALITY_QUARANTINE`` audit row the caller appended before
    invoking this.
    """
    if detected_at_utc is None:
        detected_at_utc = datetime.now(tz=UTC)
    raw_payload: dict[str, Any]
    if isinstance(raw_record, str):
        raw_payload = {"raw_line": raw_record}
    else:
        raw_payload = raw_record
    inserted = (
        await session.execute(
            text(
                "INSERT INTO data_quality_events ("
                "    market, detected_at_utc, event_type, reason, raw_bar, "
                "    audit_event_uuid"
                ") VALUES ("
                "    :market, :detected_at_utc, :event_type, :reason, "
                "    CAST(:raw_bar AS jsonb), :audit_event_uuid"
                ") RETURNING id"
            ),
            {
                "market": market,
                "detected_at_utc": detected_at_utc,
                "event_type": event_type,
                "reason": reason,
                "raw_bar": _to_canonical_json_string(raw_payload),
                "audit_event_uuid": audit_event_uuid,
            },
        )
    ).one()
    return UUID(str(inserted.id))


def _to_canonical_json_string(payload: dict[str, Any]) -> str:
    """Serialize a Python dict to JSON-as-text for the ``jsonb`` CAST.

    Decimal-as-string (A05): asyncpg's jsonb codec rejects ``Decimal``;
    we coerce via ``json.dumps(default=str)`` which serializes Decimals
    as JSON strings (matching the dev-guide §3.8 wire-Decimal pattern).
    """
    import json

    return json.dumps(payload, default=str, sort_keys=True)


__all__ = [
    "SignalInsertRow",
    "apply_cursor_advance",
    "fetch_active_account_id",
    "fetch_cursor_row",
    "fetch_slippage_head_version_id",
    "insert_data_quality_event",
    "insert_signal_row",
    "insert_slippage_bootstrap_row",
]
