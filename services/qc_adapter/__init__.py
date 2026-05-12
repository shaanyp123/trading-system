"""services/qc_adapter package — QC ObjectStore poll + ingestion service.

Phase 1 bridges the QC algorithm and the backend audit/signals tables.
Pure-policy planning lives in ``payloads.py`` / ``cursor.py`` / ``poll.py``
(Week 3 Tue scaffold); the HTTP fetcher + orchestrator + signal-ingestion
land Day 28 (Week 7 Thu) as PR-A of the Phase-1-onset 5-PR build plan.

Public API:

    # Pure-policy (Week 3 Tue) -----------------------------------------------
    payloads.QCEvent             -- parsed event ready for audit ingest
    payloads.MalformedRecord     -- parse failure for data_quality_events
    payloads.parse_jsonl_batch   -- JSONL bytes -> list of QCEvent | MalformedRecord
    cursor.CursorRow             -- one row of qc_adapter_cursor
    cursor.CursorAdvance         -- the diff to apply post-poll
    cursor.CursorDirectory       -- the three locked directories from §3.19
    cursor.POLL_CADENCE_SECONDS  -- 60s / 60s / 5s per spec §1.4
    poll.IngestPlan              -- the orchestrator's playbook
    poll.plan_ingest_batch       -- compute plan from (cursor, response_body)

    # HTTP fetcher + orchestrator + signal ingestion (Day 28 / PR-A) --------
    qc_objectstore_client.QCObjectStoreClient -- async HTTP client over QC's
                                                 ObjectStore REST API
    qc_objectstore_client.QCObjectStoreError  -- terminal-failure exception class
    orchestrator.Orchestrator     -- run_forever / run_once polling loop
    orchestrator.CycleResult      -- per-cycle diagnostics (used by tests)
    signal_ingestion.ingest_signal_emitted -- handler for SIGNAL_EMITTED events
    signal_ingestion.SignalIngestError     -- payload-shape rejection class
    config.QCAdapterSettings      -- typed pydantic-settings env loader

See module docstrings for the per-component contract; see
``deploy/qc_adapter/README.md`` for the operator runbook.
"""

from .config import QCAdapterSettings, get_settings
from .cursor import (
    POLL_CADENCE_SECONDS,
    CursorAdvance,
    CursorDirectory,
    CursorRow,
    initial_cursor_rows,
    plan_cursor_advance_failure,
    plan_cursor_advance_success,
)
from .orchestrator import CycleResult, Orchestrator
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
from .qc_objectstore_client import (
    QCObjectStoreAuthError,
    QCObjectStoreClient,
    QCObjectStoreError,
    QCObjectStoreNotFoundError,
)
from .signal_ingestion import (
    IngestResult,
    SignalIngestError,
    ingest_signal_emitted,
)

__all__ = [
    "POLL_CADENCE_SECONDS",
    "CursorAdvance",
    "CursorDirectory",
    "CursorRow",
    "CycleResult",
    "IngestPlan",
    "IngestResult",
    "MalformedPayloadError",
    "MalformedRecord",
    "Orchestrator",
    "QCAdapterSettings",
    "QCEvent",
    "QCObjectStoreAuthError",
    "QCObjectStoreClient",
    "QCObjectStoreError",
    "QCObjectStoreNotFoundError",
    "SignalIngestError",
    "get_settings",
    "ingest_signal_emitted",
    "initial_cursor_rows",
    "parse_jsonl_batch",
    "parse_jsonl_record",
    "plan_cursor_advance_failure",
    "plan_cursor_advance_success",
    "plan_ingest_batch",
]
