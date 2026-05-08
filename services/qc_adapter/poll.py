"""QC adapter ingest planner — pure-policy module (backend-spec §2.10.1).

Composes ``payloads.parse_jsonl_batch`` + ``cursor.plan_cursor_advance_*``
into a single plan-then-apply function the orchestrator (Week 4) calls
once per poll cycle. The orchestrator is responsible for the actual HTTP
fetch + DB writes; this module returns an ``IngestPlan`` describing what
to do.

Same module-shape design as ``services/scheduler/calendar_import.py``:
pure-function, deterministic, returns plan structs as data. Tests in
``tests/unit/test_qc_adapter_poll.py`` cover each branch.

Plan-then-apply contract:

    1. Caller polls QC ObjectStore via authenticated REST (Week 4 work).
    2. Caller reads the SELECT-ed cursor row into ``CursorRow``.
    3. Caller calls ``plan_ingest_batch(...)`` with the response body
       (or None on HTTP failure).
    4. Caller iterates ``IngestPlan.audit_events`` and appends each via
       ``services.audit.writer.append_audit_event(...)`` (Week 4 work).
    5. Caller iterates ``IngestPlan.malformed_records`` and writes each
       to ``data_quality_events`` (Week 4 work).
    6. Caller UPDATEs ``qc_adapter_cursor`` from ``IngestPlan.cursor_advance``.
    7. Caller sleeps ``IngestPlan.next_poll_in_seconds`` then loops.

Why all I/O sits at the caller:
- Anti-pattern A22 (unit tests must NOT write audit events): pure-policy
  modules have zero audit/SSE coupling so tests are trivial.
- The audit writer (``services/audit/writer.py``) doesn't exist yet at
  Day 7. Coupling this module to that interface would force stubbing.
- The plan struct IS the spec contract. A reviewer can inspect what the
  poll cycle WILL do without running it.

§6.8 / A27 note: this module is pure policy with zero platform calls.
The smoke-test obligation transfers to the Week 4 PR introducing the
HTTP fetcher (``services/qc_adapter/fetcher.py`` or similar). That PR
must include either (a) a CI fixture polling a known QC ObjectStore
key, OR (b) a ``deploy/qc_adapter/README.md`` operator-runbook
checklist. See decisions-log 2026-05-07 Day 5 close-out (PR #25) for the
canonical pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from .cursor import (
    POLL_CADENCE_SECONDS,
    CursorAdvance,
    CursorRow,
    plan_cursor_advance_failure,
    plan_cursor_advance_success,
)
from .payloads import (
    MalformedRecord,
    QCEvent,
    parse_jsonl_batch,
)


@dataclass(frozen=True, slots=True)
class IngestPlan:
    """The orchestrator's playbook for a single poll cycle.

    Returned by ``plan_ingest_batch``. All fields are pre-computed; the
    caller just iterates and writes.
    """

    cursor_advance: CursorAdvance
    audit_events: tuple[QCEvent, ...]
    malformed_records: tuple[MalformedRecord, ...]
    next_poll_in_seconds: int


def plan_ingest_batch(
    *,
    cursor: CursorRow,
    response_body: str | None,
    now_utc: datetime,
) -> IngestPlan:
    """Compute the ingest plan for a single poll response.

    Parameters
    ----------
    cursor:
        The current row from ``qc_adapter_cursor`` (caller SELECT-ed it
        before the HTTP fetch).
    response_body:
        The raw JSONL bytes from QC ObjectStore as ``str``, OR ``None``
        if the HTTP fetch failed (non-2xx, timeout, DNS failure, TLS
        handshake failure). The orchestrator decides which case applies
        based on its HTTP client's response/exception; this module just
        sees one or the other.
    now_utc:
        Wall-clock for cursor.last_consumed_at_utc. Caller passes
        ``datetime.now(tz=UTC)`` (anti-pattern A06: must be tz-aware).

    Returns
    -------
    IngestPlan
        - On HTTP failure: cursor advance increments
          ``consecutive_failures`` only; ``audit_events`` and
          ``malformed_records`` are empty.
        - On HTTP success with empty body: cursor's
          ``last_consumed_at_utc`` updates and counter resets, but
          sequence_no doesn't move.
        - On HTTP success with records: every parsed record is in
          ``audit_events``; every parse-failed record is in
          ``malformed_records``; cursor advances to the max observed
          sequence_no across the parsed records.

    Raises
    ------
    ValueError
        If ``now_utc`` is naive (no tzinfo). Anti-pattern A06 enforcement.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware (anti-pattern A06)")

    cadence = POLL_CADENCE_SECONDS[cursor.directory_path]

    if response_body is None:
        return IngestPlan(
            cursor_advance=plan_cursor_advance_failure(cursor=cursor),
            audit_events=(),
            malformed_records=(),
            next_poll_in_seconds=cadence,
        )

    # Successful HTTP fetch — body may be empty, fully-parsed, or mixed.
    parsed = parse_jsonl_batch(response_body)
    audit_events: tuple[QCEvent, ...] = tuple(rec for rec in parsed if isinstance(rec, QCEvent))
    malformed_records: tuple[MalformedRecord, ...] = tuple(
        rec for rec in parsed if isinstance(rec, MalformedRecord)
    )

    if audit_events:
        max_seq = max(ev.sequence_no for ev in audit_events)
    else:
        max_seq = cursor.last_consumed_sequence_no

    cursor_advance = plan_cursor_advance_success(
        cursor=cursor,
        max_observed_sequence_no=max_seq,
        bytes_in_batch=len(response_body.encode("utf-8")),
        now_utc=now_utc,
    )

    return IngestPlan(
        cursor_advance=cursor_advance,
        audit_events=audit_events,
        malformed_records=malformed_records,
        next_poll_in_seconds=cadence,
    )


__all__: Final[list[str]] = [
    "IngestPlan",
    "plan_ingest_batch",
]
