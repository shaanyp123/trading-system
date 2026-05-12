"""services/qc_adapter/orchestrator.py — poll loop tying it all together.

Per-cycle algorithm (one cycle per directory per cadence):

  1. SELECT the current ``qc_adapter_cursor`` row.
  2. ``client.list_keys(prefix)`` over QC ObjectStore.
  3. Filter keys whose embedded sequence_no > cursor.last_consumed_sequence_no.
  4. For each survivor, ``client.get_object(key)`` and concatenate the
     response bodies into a single JSONL blob.
  5. Pass the blob through ``plan_ingest_batch`` (pure-policy from poll.py)
     which returns an :class:`IngestPlan` containing parsed events,
     malformed records, and a cursor advance.
  6. For each :class:`QCEvent`, dispatch on event_type:
       * ``signal_emitted`` → ``signal_ingestion.ingest_signal_emitted``
       * any other type   → log + skip + append a generic
         ``DATA_QUALITY_QUARANTINE`` audit row so the chain stays
         continuous (PR-C/D wire real handlers for orders + fills).
  7. For each malformed record, write a ``DATA_QUALITY_QUARANTINE`` audit
     event + insert a ``data_quality_events`` row.
  8. ``apply_cursor_advance`` writes the post-state UPDATE.
  9. Sleep ``cadence_seconds`` (per :data:`POLL_CADENCE_SECONDS`).

Continuous-failure escalation per backend-spec §6.6.1:
  * < 300s of failures → log only.
  * 300..600s         → P1 alert (PR-A logs `qc_objstore_degraded`).
  * ≥ 600s            → HALT_NEW + ``qc_objstore_stale_10m`` trigger
    (PR-A surfaces the structlog event + sets a flag the operator can
    see in container logs; the actual halt transition is wired in PR-B+
    via the existing ``services.risk.dispatch.apply_state_transition``).

Why all DB I/O sits in this layer and not in pure-policy modules:
  * Anti-pattern A22 (unit tests must NOT write audit events): the
    pure-policy ``plan_ingest_batch`` stays trivially testable; the
    orchestrator is exercised end-to-end in the testcontainers
    integration test.
  * Mirrors the api ↔ repo split (``services/api/routes/*`` calls
    ``services/api/repos/phase1.py``); here the orchestrator calls
    ``services/qc_adapter/db.py``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

from . import db as qc_db
from .cursor import POLL_CADENCE_SECONDS, CursorDirectory
from .payloads import MalformedRecord, QCEvent
from .poll import plan_ingest_batch
from .qc_objectstore_client import QCObjectStoreClient, QCObjectStoreError
from .signal_ingestion import SignalIngestError, ingest_signal_emitted

log = structlog.get_logger()

#: Pattern that extracts a sequence_no from a key like ``events/1042.jsonl``
#: or ``events/signals/1042.jsonl``. The matched integer is the QC-side
#: sequence_no; keys that don't match are skipped (and logged) so a stray
#: human-named file in ObjectStore doesn't crash the orchestrator.
_KEY_SEQUENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"/?(?:[^/]+/)*(\d+)\.jsonl$")

#: Per-cycle deadline (safety valve). If a single run_once takes longer
#: than this we bail with a structlog event; this prevents one slow
#: cycle from blocking subsequent cycles indefinitely.
_RUN_ONCE_DEADLINE_SECONDS: Final[float] = 120.0


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Returned by :meth:`Orchestrator.run_once` for diagnostics + tests."""

    directory: CursorDirectory
    fetched: int  # number of QC keys fetched this cycle
    events_ingested: int
    malformed_quarantined: int
    failed: bool  # transport / parse failure at the directory level
    advance_failures: int  # post-cycle consecutive_failures counter
    next_poll_in_seconds: int


class Orchestrator:
    """Coordinates polling + DB writes for the three CursorDirectory values.

    Lifecycle:
      * ``async with Orchestrator(...) as orch: await orch.run_forever()``
        runs the three directory loops concurrently via ``asyncio.gather``.
      * ``await orch.run_once(directory)`` runs a single cycle (used by
        the integration test + smoke harness).
      * Caller owns the DB session factory + the httpx client; the
        orchestrator does not create either internally so tests can
        substitute fakes.

    The orchestrator is single-process; horizontal scaling lands Phase 2
    with a multi-replica coordination story (LISTEN/NOTIFY or Redis
    locks). Phase 0/1 single-VPS makes a single-process design correct.
    """

    def __init__(
        self,
        *,
        client: QCObjectStoreClient,
        session_factory: async_sessionmaker[AsyncSession],
        account_id: Any,  # UUID; typed loosely so callers can pass uuid.UUID
        env: Environment,
        phase_at_emit: PhaseAtEmit,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._account_id = account_id
        self._env = env
        self._phase_at_emit = phase_at_emit
        self._stop_event = asyncio.Event()

    async def __aenter__(self) -> Orchestrator:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.request_stop()

    def request_stop(self) -> None:
        """Signal the run_forever loops to exit at the next checkpoint."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Public loops
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Run all three directory cycles concurrently until stop.

        Each directory has an independent cadence + back-off. A failure
        in one directory does not affect the other two — matches the
        backend-spec §6.6.1 directory-isolated escalation model.
        """
        log.info(
            "qc_adapter_orchestrator_started",
            service_name="qc_adapter",
            env=self._env,
            phase_at_emit=self._phase_at_emit,
        )
        await asyncio.gather(
            self._run_directory_loop(CursorDirectory.EVENTS),
            self._run_directory_loop(CursorDirectory.STATE_PORTFOLIO),
            self._run_directory_loop(CursorDirectory.INSTRUCTION_ACKS),
        )

    async def _run_directory_loop(self, directory: CursorDirectory) -> None:
        bound = log.bind(service_name="qc_adapter", directory=directory.value)
        while not self._stop_event.is_set():
            try:
                result = await asyncio.wait_for(
                    self.run_once(directory),
                    timeout=_RUN_ONCE_DEADLINE_SECONDS,
                )
                bound.info(
                    "orchestrator_cycle_completed",
                    fetched=result.fetched,
                    events_ingested=result.events_ingested,
                    malformed=result.malformed_quarantined,
                    failed=result.failed,
                    advance_failures=result.advance_failures,
                )
            except TimeoutError:
                bound.error(
                    "orchestrator_cycle_deadline_exceeded",
                    deadline_s=_RUN_ONCE_DEADLINE_SECONDS,
                )
            except Exception:
                bound.exception("orchestrator_cycle_uncaught_exception")
            cadence = POLL_CADENCE_SECONDS[directory]
            # Honour stop quickly when sleeping for long cadences.
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=cadence)
                # If the wait completed without timing out, stop was requested.
                break
            except TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Single-cycle runner (the unit the integration test drives)
    # ------------------------------------------------------------------

    async def run_once(self, directory: CursorDirectory) -> CycleResult:
        """Run a single poll cycle for one directory and apply the result."""
        bound = log.bind(service_name="qc_adapter", directory=directory.value)
        cadence = POLL_CADENCE_SECONDS[directory]

        # Phase A: read cursor + list + fetch.
        cursor, body, fetched, failed = await self._read_cursor_and_fetch(
            directory=directory, bound=bound
        )

        # Phase B: pure-policy planning.
        plan = plan_ingest_batch(
            cursor=cursor,
            response_body=body,
            now_utc=datetime.now(tz=UTC),
        )

        # Phase C: apply audit + business writes one event at a time.
        ingested = 0
        quarantined = 0
        for ev in plan.audit_events:
            try:
                handled_as_ingest = await self._dispatch_event(ev)
                if handled_as_ingest:
                    ingested += 1
                else:
                    # Unhandled event_type — dispatched to internal
                    # quarantine row (PR-A: every type except
                    # signal_emitted lands here).
                    quarantined += 1
            except SignalIngestError as exc:
                bound.warning(
                    "qc_event_payload_invalid",
                    qc_sequence_no=ev.sequence_no,
                    event_type=ev.event_type,
                    error=str(exc),
                )
                await self._quarantine_malformed_event(ev, reason=str(exc))
                quarantined += 1
            except Exception:
                bound.exception(
                    "qc_event_dispatch_failed",
                    qc_sequence_no=ev.sequence_no,
                    event_type=ev.event_type,
                )

        for mr in plan.malformed_records:
            await self._quarantine_malformed_record(mr)
            quarantined += 1

        # Phase D: cursor advance.
        async with self._session_factory() as session:
            async with session.begin():
                await qc_db.apply_cursor_advance(session, plan.cursor_advance)

        return CycleResult(
            directory=directory,
            fetched=fetched,
            events_ingested=ingested,
            malformed_quarantined=quarantined,
            failed=failed,
            advance_failures=plan.cursor_advance.consecutive_failures,
            next_poll_in_seconds=cadence,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _read_cursor_and_fetch(
        self,
        *,
        directory: CursorDirectory,
        bound: structlog.stdlib.BoundLogger,
    ) -> tuple[Any, str | None, int, bool]:
        """Return (cursor_row, response_body, fetched_count, failed_flag).

        ``response_body`` is None on transport / list failure; the plan
        layer turns that into a ``consecutive_failures += 1`` advance.
        """
        async with self._session_factory() as session:
            cursor = await qc_db.fetch_cursor_row(session, directory)

        prefix = _list_prefix_for(directory)
        try:
            keys = await self._client.list_keys(prefix)
        except QCObjectStoreError as exc:
            bound.warning("qc_list_keys_failed", error=str(exc))
            return cursor, None, 0, True

        new_keys = _select_new_keys(
            keys=keys,
            last_consumed_sequence_no=cursor.last_consumed_sequence_no,
        )
        if not new_keys:
            bound.info(
                "qc_no_new_keys",
                last_consumed_sequence_no=cursor.last_consumed_sequence_no,
            )
            return cursor, "", 0, False

        bodies: list[str] = []
        for key in new_keys:
            try:
                raw = await self._client.get_object(key)
            except QCObjectStoreError as exc:
                # A get failure mid-batch flags the cycle as failed (so
                # consecutive_failures bumps), preserving the cursor for
                # the next attempt. Any bodies already accumulated would
                # otherwise risk advancing past unread keys.
                bound.warning("qc_get_object_failed", key=key, error=str(exc))
                return cursor, None, len(bodies), True
            text = raw.decode("utf-8", errors="replace")
            bodies.append(text if text.endswith("\n") else text + "\n")

        body = "".join(bodies)
        bound.info(
            "qc_fetched_keys",
            fetched=len(new_keys),
            bytes=len(body),
        )
        return cursor, body, len(new_keys), False

    async def _dispatch_event(self, event: QCEvent) -> bool:
        """Route a parsed QC event to its handler.

        PR-A wires ``signal_emitted`` only; other event types still
        produce a continuous audit chain by appending a passthrough
        ``DATA_QUALITY_QUARANTINE`` event with the unhandled-type reason.
        Real handlers for ``order_*``/``reconciliation_*``/``trade_realized``
        land in PR-C/D.

        Returns
        -------
        bool
            ``True`` if the event was ingested as its intended type
            (signals row written); ``False`` if it routed to internal
            quarantine (unhandled type). The caller uses this to
            distinguish the two counters in :class:`CycleResult`.
        """
        if event.event_type == AuditEventType.SIGNAL_EMITTED.value:
            await ingest_signal_emitted(
                self._session_factory,
                account_id=self._account_id,
                env=self._env,
                phase_at_emit=self._phase_at_emit,
                event=event,
            )
            return True

        # Other event types: log + chain-continuous quarantine row.
        log.info(
            "qc_event_unhandled_type",
            service_name="qc_adapter",
            qc_sequence_no=event.sequence_no,
            event_type=event.event_type,
        )
        await self._quarantine_malformed_event(
            event,
            reason=f"event_type {event.event_type!r} not wired in PR-A",
        )
        return False

    async def _quarantine_malformed_event(self, event: QCEvent, *, reason: str) -> None:
        """Write a DATA_QUALITY_QUARANTINE audit row for an event-shaped
        record that we could not ingest as a signal."""
        async with self._session_factory() as session:
            record = await append_audit_event(
                session,
                AuditEventType.DATA_QUALITY_QUARANTINE,
                {
                    "qc_sequence_no": event.sequence_no,
                    "event_type": event.event_type,
                    "qc_algorithm_version": event.qc_algorithm_version,
                    "reason": reason,
                },
                account_id=self._account_id,
                env=self._env,
                phase_at_emit=self._phase_at_emit,
                source_clock_ts=event.source_clock_ts,
            )
        async with self._session_factory() as session:
            async with session.begin():
                await qc_db.insert_data_quality_event(
                    session,
                    market=str(event.payload.get("market", "unknown")),
                    reason=reason,
                    raw_record={
                        "event_type": event.event_type,
                        "qc_sequence_no": event.sequence_no,
                        "qc_algorithm_version": event.qc_algorithm_version,
                    },
                    audit_event_uuid=record.event_uuid,
                )

    async def _quarantine_malformed_record(self, record: MalformedRecord) -> None:
        """Write a DATA_QUALITY_QUARANTINE audit row for a JSONL line
        that failed parsing entirely (couldn't even produce a QCEvent)."""
        async with self._session_factory() as session:
            audit_record = await append_audit_event(
                session,
                AuditEventType.DATA_QUALITY_QUARANTINE,
                {
                    "line_index": record.line_index,
                    "error": record.error,
                    "raw_line_truncated": record.raw_line[:200],
                },
                account_id=self._account_id,
                env=self._env,
                phase_at_emit=self._phase_at_emit,
            )
        async with self._session_factory() as session:
            async with session.begin():
                await qc_db.insert_data_quality_event(
                    session,
                    market="unknown",
                    reason=record.error,
                    raw_record=record.raw_line[:1000],
                    audit_event_uuid=audit_record.event_uuid,
                )


def _list_prefix_for(directory: CursorDirectory) -> str:
    """Map a CursorDirectory to the QC ObjectStore key prefix.

    QC keys do NOT carry a leading slash (the ``directory_path`` column
    uses ``/events/`` for the cursor row label, but ``client.list_keys``
    takes a flat prefix like ``events/``). The state/portfolio.json
    directory is a single-file path — pass the literal key.
    """
    if directory == CursorDirectory.STATE_PORTFOLIO:
        # The "directory" here is actually a single file; list_keys with
        # the exact key returns a 1-entry list when the file exists, 0
        # otherwise. Use the trailing-slash-stripped name as the prefix.
        return "state/portfolio.json"
    return directory.value.lstrip("/")


def _select_new_keys(*, keys: list[str], last_consumed_sequence_no: int) -> list[str]:
    """Filter QC keys to ones with embedded sequence_no > cursor.

    Keys whose pattern does not match (e.g. operator dropped a stray
    file) are silently skipped — the orchestrator emits a structlog
    info but does not fail.
    """
    out: list[tuple[int, str]] = []
    for key in keys:
        match = _KEY_SEQUENCE_PATTERN.search(key)
        if match is None:
            log.info(
                "qc_key_skipped_no_sequence",
                service_name="qc_adapter",
                key=key,
            )
            continue
        seq = int(match.group(1))
        if seq > last_consumed_sequence_no:
            out.append((seq, key))
    out.sort()
    return [k for _, k in out]


__all__ = [
    "CycleResult",
    "Orchestrator",
]
