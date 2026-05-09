"""Audit-log domain models — return-type DTOs for the writer.

The writer (:func:`services.audit.writer.append_audit_event`) returns an
:class:`AuditLogRecord` after a successful insert. Callers chain the
``event_uuid`` and ``sequence_no`` into downstream artifacts (SSE
``audit_event_uuid`` field, ``risk_state.audit_event_uuid`` FK, etc.) without
re-reading the row.

The full ``audit_log`` row carries more fields (``prev_hash``, ``record_hash``,
``payload_jcs`` BYTEA, etc.); those are not exposed here because:

* ``record_hash`` and ``prev_hash`` are chain-internal — verifying the chain
  is the job of :func:`services.audit.chain.verify_chain`, not of any
  per-write caller.
* ``payload_jcs`` is the canonical bytes blob; callers already have the
  pre-canonicalization ``payload`` dict they passed in.

If a future caller needs richer access, add fields here rather than
encouraging direct ``audit_log`` reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .event_types import AuditEventType


@dataclass(frozen=True, slots=True)
class AuditLogRecord:
    """Result of a successful :func:`append_audit_event` call.

    All fields come from the ``RETURNING`` clause of the INSERT — the
    writer never reads back from ``audit_log`` after the insert.

    Attributes
    ----------
    sequence_no:
        Globally monotonic ``BIGSERIAL`` from the audit_log sequence. Not
        re-used across partitions or transactions.
    event_uuid:
        The caller-provided or writer-generated ``uuid7`` for this event.
        Use this as the foreign-key reference from any business table that
        needs to point at the audit record (e.g. ``risk_state.audit_event_uuid``).
    event_type:
        The taxonomy enum value emitted. Always normalized to the enum form
        even if the caller passed a raw string.
    ingest_clock_ts:
        Wall-clock at the time the row was committed. Authoritative for the
        partition key.
    """

    sequence_no: int
    event_uuid: UUID
    event_type: AuditEventType
    ingest_clock_ts: datetime


__all__ = ["AuditLogRecord"]
