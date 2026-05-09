"""Audit-log writer — the single canonical entry point for ``audit_log`` rows.

Implements backend-spec §2.10.1 verbatim: SERIALIZABLE isolation +
``pg_advisory_xact_lock`` on a fixed app-defined bigint, exponential-backoff
retry on serialization failure (SQLSTATE 40001), HALT_NEW signal after 5
exhausted retries.

Anti-pattern A01 (DO NOT write to ``audit_log`` directly via INSERT) binds:
every audit-bearing module routes through :func:`append_audit_event`.

Why both an advisory lock AND SERIALIZABLE
-------------------------------------------

The advisory lock (``pg_advisory_xact_lock``) serializes concurrent writers
at the application layer. Without it, two concurrent SERIALIZABLE
transactions would both read the same ``prev_hash`` from the most-recent
row and both attempt to INSERT — Postgres detects the conflict and aborts
one with ``serialization_failure``. The lock prevents the wasted work of
the second transaction running to completion only to be rolled back.

SERIALIZABLE is retained as the second line of defense: if the advisory
lock is somehow bypassed (or a future migration changes the lock id), the
SSI guarantees still catch the read-modify-write hazard.

The retry loop (5 attempts, exponential backoff: 10ms / 50ms / 250ms /
1.25s / 6s) handles the rare case where SSI does abort despite the lock —
e.g. an unrelated transaction reading from ``audit_log`` while we hold
the lock could trigger a phantom-read predicate clash. After 5 retries
the writer raises :class:`AuditWriteFailure`; the dispatch layer (when it
lands) is expected to catch this and invoke
``services.risk.state_machine.plan_invoke_kill_switch`` with trigger
``AUDIT_WRITE_FAIL`` (severity ``incident_review`` per
TRIGGER_SEVERITY).

This module is **pure I/O** — the policy of "5 retries then HALT" is set
here, but the actual HALT_NEW transition is the dispatch layer's job. We
deliberately do not import ``services.risk`` from here so the writer stays
independently testable against a fresh testcontainer.

Caller contract
---------------

    >>> record = await append_audit_event(
    ...     session,
    ...     AuditEventType.SYSTEM_STARTED,
    ...     {"version": "1.0.0"},
    ...     account_id=account_id,
    ...     env="paper",
    ...     phase_at_emit=0,
    ... )
    >>> record.sequence_no
    1

The caller commits/rolls-back the surrounding session at the boundaries it
controls; this writer manages its own transaction internally via
``async with session.begin()`` so the SERIALIZABLE level + advisory lock
are correctly scoped. Callers MUST NOT pass a session that already has an
open transaction.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

import structlog
import uuid_utils as uuid7_lib
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .chain import GENESIS_HASH, compute_record_hash, jcs_serialize
from .event_types import AuditEventType
from .models import AuditLogRecord

log = structlog.get_logger()

#: Application-defined fixed bigint for ``pg_advisory_xact_lock``.
#: Hex-encoded ASCII "auditcha" — same value referenced (as a comment) in
#: ``alembic/versions/0001_audit_log.py`` so spec, migration, and writer
#: all use the SAME lock key. Changing this value would let pre-change
#: writers and post-change writers operate concurrently and corrupt the
#: chain — treat it as load-bearing.
AUDIT_CHAIN_LOCK_ID: Final[int] = 0x6175646974636861

#: Backoff schedule in seconds. Five attempts total (0..4); after attempt 4
#: the writer gives up and raises :class:`AuditWriteFailure`.
RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.05, 0.25, 1.25, 6.0)

#: Maximum number of attempts before failing closed.
MAX_RETRIES: Final[int] = len(RETRY_DELAYS_SECONDS)

#: Postgres SQLSTATE for serialization failure under SERIALIZABLE isolation.
#: SQLAlchemy surfaces this as :class:`OperationalError` with this pgcode.
_SERIALIZATION_FAILURE_PGCODE: Final[str] = "40001"


Environment = Literal["paper", "live-small", "live-scale"]
PhaseAtEmit = Literal[0, 1, 2, 3]


class AuditWriteFailure(Exception):
    """Raised after :data:`MAX_RETRIES` exhausted SERIALIZABLE retries.

    The dispatch layer is expected to catch this and route the failure
    through ``plan_invoke_kill_switch(trigger=AUDIT_WRITE_FAIL)``. We do
    not invoke that transition from here to keep the writer independently
    testable and to avoid an import cycle with ``services.risk``.
    """


def _normalize_event_type(event_type: AuditEventType | str) -> AuditEventType:
    """Coerce a raw string event-type to the locked enum.

    PR #28 modules emit strings (``Literal["state_transition_normal_to_halt", ...]``)
    because this enum did not yet exist when those PRs landed. Accepting both
    forms here means those modules don't need to be retrofit. Strings outside
    the locked taxonomy raise :class:`ValueError` per anti-pattern A04.
    """
    if isinstance(event_type, AuditEventType):
        return event_type
    try:
        return AuditEventType(event_type)
    except ValueError as exc:
        raise ValueError(
            f"event_type {event_type!r} is not in the locked taxonomy "
            f"(backend-spec §3.30 / services.audit.event_types.AuditEventType). "
            f"Anti-pattern A04: introduce new event types via spec migration + PR."
        ) from exc


def _is_serialization_failure(error: DBAPIError) -> bool:
    """Detect Postgres ``serialization_failure`` (SQLSTATE 40001).

    asyncpg's :class:`asyncpg.exceptions.SerializationError` extends
    ``PostgresError`` (not a more specific class) and is therefore
    translated by the SQLAlchemy asyncpg dialect's
    ``_asyncpg_error_translate`` map to the generic dialect ``Error``
    class, NOT to ``OperationalError``. Detect by SQLSTATE on ``.orig``
    instead of by exception class.
    """
    orig = getattr(error, "orig", None)
    if orig is None:
        return False
    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    return pgcode == _SERIALIZATION_FAILURE_PGCODE


async def append_audit_event(
    session: AsyncSession,
    event_type: AuditEventType | str,
    payload: dict[str, Any],
    *,
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
    source_clock_ts: datetime | None = None,
    monotonic_ns: int | None = None,
    event_uuid: UUID | None = None,
    repaired_for_sequence_no: int | None = None,
    repaired_for_event_timestamp: datetime | None = None,
) -> AuditLogRecord:
    """Append a row to ``audit_log`` with hash-chain integrity.

    Concurrency: ``pg_advisory_xact_lock`` on :data:`AUDIT_CHAIN_LOCK_ID`
    serializes concurrent writers; SERIALIZABLE isolation guards the
    prev-hash read + insert as a single atomic critical section.

    Retry: up to :data:`MAX_RETRIES` attempts on serialization failure,
    backed off per :data:`RETRY_DELAYS_SECONDS`. Beyond that, raises
    :class:`AuditWriteFailure` — the caller's dispatch layer routes that
    to HALT_NEW (incident_review).

    Idempotency: if a future migration adds ``UNIQUE(event_uuid)`` and a
    duplicate UUID is rejected, the writer reads back the existing row and
    returns it without raising. The current schema (alembic 0001) has only
    a non-unique index on ``event_uuid`` so this branch is defensive — the
    current behavior on a reused UUID is to insert a second row with the
    same UUID. Pass a fresh UUID per logical event.

    Parameters
    ----------
    session:
        SQLAlchemy async session WITHOUT an open transaction. The writer
        opens its own transaction internally so isolation level + advisory
        lock are correctly scoped.
    event_type:
        Member of :class:`AuditEventType` (preferred) or a raw string that
        is a value of that enum. Strings outside the taxonomy raise
        ``ValueError``.
    payload:
        Event-specific data. Will be JCS-canonicalized; must contain only
        ``str`` / ``int`` / ``bool`` / ``None`` / :class:`~decimal.Decimal`
        / nested ``dict`` / ``list``. Floats raise ``TypeError`` per
        anti-pattern A05.
    account_id:
        ``audit_log.account_id`` foreign key. Caller's responsibility to
        ensure the account exists (FK enforced post-0002 migration).
    env:
        ``audit_log.env`` — one of ``paper``, ``live-small``, ``live-scale``.
    phase_at_emit:
        ``audit_log.phase_at_emit`` — 0, 1, 2, or 3 (CHECK constraint).
    source_clock_ts:
        Defaults to ``datetime.now(tz=UTC)``. Pass an explicit timestamp
        for replay or backfill scenarios where the source event is older
        than wall-clock now.
    monotonic_ns:
        Defaults to ``time.monotonic_ns()`` at call time. Useful for
        ordering events within a process; not authoritative across
        processes (each has its own monotonic origin).
    event_uuid:
        Defaults to a fresh ``uuid7``. Pass an explicit UUID when the
        caller has already minted one for FK chaining (e.g. when a
        business row with ``audit_event_uuid`` will be written by the
        same caller in a subsequent transaction).
    repaired_for_sequence_no, repaired_for_event_timestamp:
        Set together when this row is a backfill repair for a previously
        lost row in the chain. See backend-spec §2.11 ("Loss handling").
    """
    enum_event_type = _normalize_event_type(event_type)

    if event_uuid is None:
        event_uuid = UUID(str(uuid7_lib.uuid7()))
    if source_clock_ts is None:
        source_clock_ts = datetime.now(tz=UTC)
    if monotonic_ns is None:
        monotonic_ns = time.monotonic_ns()

    canonical_payload: bytes = jcs_serialize(payload)

    last_serialization_error: DBAPIError | None = None

    for attempt in range(MAX_RETRIES):
        try:
            async with session.begin():
                # SERIALIZABLE must be set as the FIRST statement of the
                # transaction, before any data-touching query.
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": AUDIT_CHAIN_LOCK_ID},
                )

                tail_row = (
                    await session.execute(
                        text("SELECT record_hash FROM audit_log ORDER BY sequence_no DESC LIMIT 1")
                    )
                ).fetchone()
                prev_hash: bytes = bytes(tail_row.record_hash) if tail_row else GENESIS_HASH
                record_hash: bytes = compute_record_hash(prev_hash, canonical_payload)

                inserted = (
                    await session.execute(
                        text(
                            "INSERT INTO audit_log ("
                            "    event_uuid, event_type, account_id, env, "
                            "    phase_at_emit, source_clock_ts, monotonic_ns, "
                            "    prev_hash, record_hash, payload_jcs, "
                            "    repaired_for_sequence_no, repaired_for_event_timestamp"
                            ") VALUES ("
                            "    :event_uuid, :event_type, :account_id, :env, "
                            "    :phase_at_emit, :source_clock_ts, :monotonic_ns, "
                            "    :prev_hash, :record_hash, :payload_jcs, "
                            "    :repaired_for_sequence_no, :repaired_for_event_timestamp"
                            ") RETURNING sequence_no, event_uuid, ingest_clock_ts"
                        ),
                        {
                            "event_uuid": event_uuid,
                            "event_type": enum_event_type.value,
                            "account_id": account_id,
                            "env": env,
                            "phase_at_emit": phase_at_emit,
                            "source_clock_ts": source_clock_ts,
                            "monotonic_ns": monotonic_ns,
                            "prev_hash": prev_hash,
                            "record_hash": record_hash,
                            "payload_jcs": canonical_payload,
                            "repaired_for_sequence_no": repaired_for_sequence_no,
                            "repaired_for_event_timestamp": repaired_for_event_timestamp,
                        },
                    )
                ).one()

            log.info(
                "audit_event_appended",
                event_type=enum_event_type.value,
                sequence_no=inserted.sequence_no,
                event_uuid=str(event_uuid),
                attempt=attempt,
                monotonic_ns=monotonic_ns,
            )
            return AuditLogRecord(
                sequence_no=inserted.sequence_no,
                event_uuid=event_uuid,
                event_type=enum_event_type,
                ingest_clock_ts=inserted.ingest_clock_ts,
            )

        except IntegrityError as exc:
            existing = await _read_existing_by_event_uuid(session, event_uuid)
            if existing is not None:
                log.info(
                    "audit_event_already_exists",
                    event_type=enum_event_type.value,
                    event_uuid=str(event_uuid),
                    sequence_no=existing.sequence_no,
                )
                return existing
            log.error(
                "audit_event_integrity_error",
                event_type=enum_event_type.value,
                event_uuid=str(event_uuid),
                error=str(exc.orig),
            )
            raise

        except DBAPIError as exc:
            if not _is_serialization_failure(exc):
                raise
            last_serialization_error = exc
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS_SECONDS[attempt]
                log.warning(
                    "audit_event_serialization_retry",
                    event_type=enum_event_type.value,
                    event_uuid=str(event_uuid),
                    attempt=attempt,
                    next_delay_seconds=delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    log.error(
        "audit_event_serialization_exhausted",
        event_type=enum_event_type.value,
        event_uuid=str(event_uuid),
        attempts=MAX_RETRIES,
    )
    raise AuditWriteFailure(
        f"audit_log write failed {MAX_RETRIES} times under SERIALIZABLE; "
        f"caller must transition risk_state to HALT_NEW "
        f"(trigger=AUDIT_WRITE_FAIL, severity=incident_review)"
    ) from last_serialization_error


async def _read_existing_by_event_uuid(
    session: AsyncSession,
    event_uuid: UUID,
) -> AuditLogRecord | None:
    """Read back an existing audit_log row by ``event_uuid`` (idempotency path).

    Used by :func:`append_audit_event` when an :class:`IntegrityError` would
    otherwise propagate. Returns ``None`` if no matching row exists (in
    which case the IntegrityError is re-raised).
    """
    row = (
        await session.execute(
            text(
                "SELECT sequence_no, event_uuid, event_type, ingest_clock_ts "
                "FROM audit_log WHERE event_uuid = :event_uuid "
                "ORDER BY sequence_no ASC LIMIT 1"
            ),
            {"event_uuid": event_uuid},
        )
    ).fetchone()
    if row is None:
        return None
    return AuditLogRecord(
        sequence_no=row.sequence_no,
        event_uuid=row.event_uuid,
        event_type=AuditEventType(row.event_type),
        ingest_clock_ts=row.ingest_clock_ts,
    )


__all__ = [
    "AUDIT_CHAIN_LOCK_ID",
    "MAX_RETRIES",
    "RETRY_DELAYS_SECONDS",
    "AuditWriteFailure",
    "Environment",
    "PhaseAtEmit",
    "append_audit_event",
]
