"""QC adapter cursor — pure-policy module (backend-spec §3.19).

Models the ``qc_adapter_cursor`` table as immutable Python dataclasses and
exposes plan-then-apply helpers that compute the cursor advance for a
poll cycle. The caller (Week 4 orchestrator) issues the actual
``UPDATE qc_adapter_cursor SET ... WHERE directory_path = ...`` SQL.

Same module-shape design as ``services/risk/sizing.py`` and
``services/risk/state_machine.py``: the policy is a synchronous,
deterministic plan-then-apply function. Tests in
``tests/unit/test_qc_adapter_cursor.py`` cover each branch.

Spec reference (§3.19):

    CREATE TABLE qc_adapter_cursor (
      directory_path TEXT PRIMARY KEY,
      last_consumed_sequence_no BIGINT NOT NULL,
      last_consumed_at_utc TIMESTAMPTZ NOT NULL,
      bytes_consumed BIGINT NOT NULL DEFAULT 0,
      consecutive_failures INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO qc_adapter_cursor VALUES
      ('/events/', 0, now(), 0, 0),
      ('/state/portfolio.json', 0, now(), 0, 0),
      ('/instruction_acks/', 0, now(), 0, 0);

Why three directories with separate cursors:
- ``/events/`` is the audit firehose — strategy decisions, fills, state
  transitions. 60s poll cadence; sequence_no must be monotonic per
  backend-spec §2.10.1 (audit chain).
- ``/state/portfolio.json`` is a single-file snapshot, not append-only.
  60s poll cadence (matches the algorithm's 60s portfolio push). Cursor
  semantics here are "modified-time" rather than sequence_no, but we keep
  the same row shape so the schema stays uniform.
- ``/instruction_acks/`` is the response channel for the backend's
  defensive-trim instruction protocol (backend-spec §4.5.2). 5s poll
  cadence so a fill/no-fill round-trip stays under the spec's <20s p99
  budget.

Cursor advance semantics (this module):
- Successful poll with N events ingested:
  * ``last_consumed_sequence_no`` += max sequence_no observed in batch
  * ``last_consumed_at_utc`` = now_utc
  * ``bytes_consumed`` += observed batch byte count
  * ``consecutive_failures`` = 0  (any successful poll resets the counter)
- Failed poll (HTTP error, parse error on entire batch, etc.):
  * ``last_consumed_sequence_no`` unchanged
  * ``last_consumed_at_utc`` unchanged (only successful polls update it)
  * ``bytes_consumed`` unchanged
  * ``consecutive_failures`` += 1
- Successful poll with zero events (the QC algorithm hasn't emitted yet):
  * Same as successful-with-events but with no sequence_no advance.
  * Counter reset is the important property: a long quiet period followed
    by an empty-but-successful poll should not look like a degraded
    service.

§6.8 / A27 note: this module is pure policy, no I/O. The smoke-test
obligation transfers to the Week 4 PR that introduces the actual HTTP
fetcher (see ``payloads.py`` module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final


class CursorDirectory(StrEnum):
    """The three canonical cursor directories, locked by spec §3.19.

    Adding a fourth directory requires (a) an alembic migration to add the
    INSERT row to ``qc_adapter_cursor`` and (b) updating this enum. Per
    dev-guide §11 [A02], that migration touches ``alembic/**`` so it
    requires the ``risk-review-approved`` label.
    """

    EVENTS = "/events/"
    STATE_PORTFOLIO = "/state/portfolio.json"
    INSTRUCTION_ACKS = "/instruction_acks/"


#: Per-directory poll cadence in seconds. Matches the diagram in
#: backend-spec §1.4 (line 105: "Poll /events 60s; Poll /acks 5s") and the
#: 60s portfolio-push cadence from §2.4 ("Every 60s during CME session").
POLL_CADENCE_SECONDS: Final[dict[CursorDirectory, int]] = {
    CursorDirectory.EVENTS: 60,
    CursorDirectory.STATE_PORTFOLIO: 60,
    CursorDirectory.INSTRUCTION_ACKS: 5,
}


@dataclass(frozen=True, slots=True)
class CursorRow:
    """One row of ``qc_adapter_cursor``, as the caller hands it to us.

    Mirrors the §3.19 schema. The caller hydrates this dataclass from a
    SELECT before calling ``plan_cursor_advance``; after the policy
    returns, the caller writes the plan back via UPDATE.
    """

    directory_path: CursorDirectory
    last_consumed_sequence_no: int
    last_consumed_at_utc: datetime
    bytes_consumed: int
    consecutive_failures: int


@dataclass(frozen=True, slots=True)
class CursorAdvance:
    """The diff to apply to a ``qc_adapter_cursor`` row after a poll.

    Returned by ``plan_cursor_advance``. The caller translates this into
    a single UPDATE; we deliberately return the full post-state (not a
    partial diff) so the UPDATE is idempotent under retry.
    """

    directory_path: CursorDirectory
    last_consumed_sequence_no: int
    last_consumed_at_utc: datetime
    bytes_consumed: int
    consecutive_failures: int


def plan_cursor_advance_success(
    *,
    cursor: CursorRow,
    max_observed_sequence_no: int,
    bytes_in_batch: int,
    now_utc: datetime,
) -> CursorAdvance:
    """Compute the post-state of the cursor after a successful poll.

    A "successful poll" means the HTTP fetch returned 2xx and at least
    one record parsed (or zero, for an empty-but-OK poll). Caller passes
    ``max_observed_sequence_no`` = ``cursor.last_consumed_sequence_no``
    when the batch is empty, so the cursor doesn't go backward.

    Raises ``ValueError`` if ``max_observed_sequence_no`` is less than
    ``cursor.last_consumed_sequence_no`` (sequence_no is monotonic per
    backend-spec §2.10.1 — going backward indicates a bug or a real QC
    side malfunction that should fail loudly, not silently corrupt the
    cursor).

    Resets ``consecutive_failures`` to 0.
    """
    if max_observed_sequence_no < cursor.last_consumed_sequence_no:
        raise ValueError(
            f"sequence_no regressed: cursor at "
            f"{cursor.last_consumed_sequence_no}, batch max "
            f"{max_observed_sequence_no} (backend-spec §2.10.1 monotonicity)"
        )
    if bytes_in_batch < 0:
        raise ValueError(f"bytes_in_batch must be >= 0, got {bytes_in_batch}")

    return CursorAdvance(
        directory_path=cursor.directory_path,
        last_consumed_sequence_no=max_observed_sequence_no,
        last_consumed_at_utc=now_utc,
        bytes_consumed=cursor.bytes_consumed + bytes_in_batch,
        consecutive_failures=0,
    )


def plan_cursor_advance_failure(*, cursor: CursorRow) -> CursorAdvance:
    """Compute the post-state of the cursor after a failed poll.

    A "failed poll" means the HTTP fetch errored (non-2xx, timeout,
    DNS failure, TLS handshake failure) OR the response body was
    structurally unparseable as JSONL (vs. a single-line parse error,
    which is data-quality, not a poll failure — those flow through
    ``plan_cursor_advance_success`` with the bad records routed to
    ``data_quality_events``).

    Increments ``consecutive_failures``; leaves sequence_no, timestamp,
    and bytes_consumed untouched (the cursor only moves forward on
    confirmed successful reads).
    """
    return replace(
        CursorAdvance(
            directory_path=cursor.directory_path,
            last_consumed_sequence_no=cursor.last_consumed_sequence_no,
            last_consumed_at_utc=cursor.last_consumed_at_utc,
            bytes_consumed=cursor.bytes_consumed,
            consecutive_failures=cursor.consecutive_failures + 1,
        )
    )


def initial_cursor_rows(*, now_utc: datetime) -> list[CursorRow]:
    """Build the three canonical INSERT rows from spec §3.19.

    Used by the alembic migration that creates ``qc_adapter_cursor`` (or
    by a test harness that needs to seed an in-memory state). The
    migration itself runs raw SQL per the dev-guide §7.1 / decisions-log
    2026-05-05 Day 3 conventions; this helper is for Python-side
    consumers.

    Note: actual table seeding is migration-territory; this helper
    intentionally returns dataclasses, not SQL.
    """
    return [
        CursorRow(
            directory_path=directory,
            last_consumed_sequence_no=0,
            last_consumed_at_utc=now_utc,
            bytes_consumed=0,
            consecutive_failures=0,
        )
        for directory in CursorDirectory
    ]


__all__ = [
    "POLL_CADENCE_SECONDS",
    "CursorAdvance",
    "CursorDirectory",
    "CursorRow",
    "initial_cursor_rows",
    "plan_cursor_advance_failure",
    "plan_cursor_advance_success",
]
