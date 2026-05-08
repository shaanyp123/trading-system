"""services/qc_adapter package — QC ObjectStore poll planner (Week 3 Tue scaffold).

Pure-policy plan-then-apply modules per backend-spec §3.19 + §4.5.1 + §2.10.1.
Network HTTP fetch + DB writes land in Week 4 alongside the audit writer
(see implementation-guide.md §3 Week 3-4 for the gate).

Public API:

    payloads.QCEvent             -- parsed event ready for audit ingest
    payloads.MalformedRecord     -- parse failure for data_quality_events
    payloads.parse_jsonl_batch   -- JSONL bytes -> list of QCEvent | MalformedRecord
    cursor.CursorRow             -- one row of qc_adapter_cursor
    cursor.CursorAdvance         -- the diff to apply post-poll
    cursor.CursorDirectory       -- the three locked directories from §3.19
    cursor.POLL_CADENCE_SECONDS  -- 60s / 60s / 5s per spec §1.4
    poll.IngestPlan              -- the orchestrator's playbook
    poll.plan_ingest_batch       -- compute plan from (cursor, response_body)

See ``services/qc_adapter/poll.py`` module docstring for the full
plan-then-apply contract the Week 4 orchestrator implements.
"""

from .cursor import (
    POLL_CADENCE_SECONDS,
    CursorAdvance,
    CursorDirectory,
    CursorRow,
    initial_cursor_rows,
    plan_cursor_advance_failure,
    plan_cursor_advance_success,
)
from .payloads import (
    MalformedPayloadError,
    MalformedRecord,
    QCEvent,
    parse_jsonl_batch,
    parse_jsonl_record,
)
from .poll import (
    IngestPlan,
    plan_ingest_batch,
)

__all__ = [
    "POLL_CADENCE_SECONDS",
    "CursorAdvance",
    "CursorDirectory",
    "CursorRow",
    "IngestPlan",
    "MalformedPayloadError",
    "MalformedRecord",
    "QCEvent",
    "initial_cursor_rows",
    "parse_jsonl_batch",
    "parse_jsonl_record",
    "plan_cursor_advance_failure",
    "plan_cursor_advance_success",
    "plan_ingest_batch",
]
