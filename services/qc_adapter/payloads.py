"""QC ObjectStore JSONL payload parser — pure data-shape module.

Implements the wire schema from backend-spec §4.5.1 (QC ObjectStore Polling
Payloads). Pure parsing + validation; no I/O. The caller (the future
``services/qc_adapter/poll.py`` orchestrator + Week 4 HTTP fetcher) reads
JSONL bytes from QC's ObjectStore REST API and feeds them through the
parser; malformed records are returned as ``MalformedRecord`` rather than
raising, so a single bad line in a batch can be quarantined to
``data_quality_events`` without poisoning the whole poll.

Same module-shape design as ``services/audit/decision_diary.py`` and
``services/scheduler/calendar_import.py``: pure-function, deterministic,
zero external dependencies. Tests in ``tests/unit/test_qc_adapter_payloads.py``
cover the schema branches.

Spec reference:
    backend-spec §4.5.1 (JSONL events schema)
    backend-spec §3.19 (qc_adapter_cursor — three canonical directories)

Wire schema (canonical):

    {
      "sequence_no": 1042,
      "event_type": "signal_emitted",
      "source_clock_ts": "2026-05-04T17:30:01.234Z",
      "qc_algorithm_version": "v1-trend-following-9d2f7a1c",
      "payload": { ... }
    }

Constraints enforced (matching the spec wire format):

- ``sequence_no``: integer >= 0 (BIGINT; QC-side-monotonic per directory).
- ``event_type``: non-empty string. The canonical taxonomy lives at
  backend-spec §3.30 (mirrored in ``services/audit/event_types.py`` when
  it lands); we deliberately do NOT validate against the taxonomy here.
  Reason: the audit writer (the layer that actually appends to
  ``audit_log``) will check the taxonomy at that boundary, and any
  unrecognized event_type from QC indicates either (a) QC algorithm code
  drift (handle as data_quality_quarantine, NOT a parse error) or (b) a
  taxonomy gap (forces an enum migration via A04). Parsing is the wrong
  layer for that distinction.
- ``source_clock_ts``: RFC 3339 / ISO 8601 with UTC offset. Parsed via
  ``datetime.fromisoformat`` (Python 3.11+ accepts trailing 'Z').
- ``qc_algorithm_version``: non-empty string. Format is QC-side
  convention (e.g. ``v1-trend-following-9d2f7a1c``); we don't pin a
  pattern.
- ``payload``: object/dict (anti-pattern A05: must NOT contain float
  fields for money/price; QC emits prices as JSON numbers — the audit
  writer converts to ``Decimal`` at the boundary, NOT here).

§6.8 / A27 note (platform smoke-test rule, locked Day 5 PR #25): this
module is pure data-shape — it does NOT call QC's ObjectStore API, so
A27 does not bind on this commit. The smoke-test obligation transfers to
the first commit that introduces the actual HTTP fetcher (planned Week 4
per implementation-guide.md §3 Week 3-4). At that point the Week 4 PR
must include either (a) a CI fixture polling a known QC ObjectStore
key, OR (b) a `deploy/qc_adapter/README.md` operator-runbook checklist
per dev-guide §6.8.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final


class MalformedPayloadError(ValueError):
    """Raised by ``parse_jsonl_record`` when a line is structurally invalid.

    ``parse_jsonl_batch`` catches these and packages them as
    ``MalformedRecord`` so a single bad line doesn't fail the whole batch.
    """


#: Required top-level keys per backend-spec §4.5.1.
REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"sequence_no", "event_type", "source_clock_ts", "qc_algorithm_version", "payload"}
)


@dataclass(frozen=True, slots=True)
class QCEvent:
    """A parsed, validated QC event ready for downstream ingestion.

    Mirrors the §4.5.1 wire schema exactly. ``payload`` is kept as the
    raw parsed dict; the audit writer (Week 4) will canonicalize via JCS
    before computing ``record_hash`` per backend-spec §2.10.1.
    """

    sequence_no: int
    event_type: str
    source_clock_ts: datetime
    qc_algorithm_version: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MalformedRecord:
    """A line that failed parsing.

    Carries enough detail to (a) write a ``data_quality_events`` row at
    the caller's boundary (Week 4) and (b) skip the line in cursor-advance
    accounting (the cursor still moves past it, but ``consecutive_failures``
    increments). Keeping the raw line allows post-mortem inspection.
    """

    raw_line: str
    error: str
    line_index: int  # 0-based index within the batch


def parse_jsonl_record(line: str) -> QCEvent:
    """Parse a single JSONL line into a ``QCEvent``.

    Raises ``MalformedPayloadError`` on any structural problem:
    invalid JSON, missing required key, wrong type for a required key,
    unparseable ``source_clock_ts``.

    Does NOT validate ``event_type`` against the audit taxonomy (see
    module docstring for rationale).
    """
    stripped = line.strip()
    if not stripped:
        raise MalformedPayloadError("empty or whitespace-only line")

    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise MalformedPayloadError(f"invalid JSON: {exc.msg} at pos {exc.pos}") from exc

    if not isinstance(obj, dict):
        raise MalformedPayloadError(f"top-level must be a JSON object, got {type(obj).__name__}")

    missing = REQUIRED_KEYS - obj.keys()
    if missing:
        raise MalformedPayloadError(f"missing required key(s): {sorted(missing)}")

    sequence_no = obj["sequence_no"]
    if not isinstance(sequence_no, int) or isinstance(sequence_no, bool):
        # ``isinstance(True, int)`` is True in Python; reject explicitly so
        # ``"sequence_no": true`` doesn't silently parse as 1.
        raise MalformedPayloadError(f"sequence_no must be int, got {type(sequence_no).__name__}")
    if sequence_no < 0:
        raise MalformedPayloadError(f"sequence_no must be >= 0, got {sequence_no}")

    event_type = obj["event_type"]
    if not isinstance(event_type, str) or not event_type:
        raise MalformedPayloadError("event_type must be a non-empty string")

    qc_algorithm_version = obj["qc_algorithm_version"]
    if not isinstance(qc_algorithm_version, str) or not qc_algorithm_version:
        raise MalformedPayloadError("qc_algorithm_version must be a non-empty string")

    source_clock_ts_raw = obj["source_clock_ts"]
    if not isinstance(source_clock_ts_raw, str):
        raise MalformedPayloadError(
            f"source_clock_ts must be a string, got {type(source_clock_ts_raw).__name__}"
        )
    try:
        source_clock_ts = datetime.fromisoformat(source_clock_ts_raw)
    except ValueError as exc:
        raise MalformedPayloadError(f"source_clock_ts is not RFC 3339 / ISO 8601: {exc}") from exc
    if source_clock_ts.tzinfo is None:
        raise MalformedPayloadError(
            "source_clock_ts must include a timezone offset (anti-pattern A06)"
        )

    payload = obj["payload"]
    if not isinstance(payload, dict):
        raise MalformedPayloadError(f"payload must be a JSON object, got {type(payload).__name__}")

    return QCEvent(
        sequence_no=sequence_no,
        event_type=event_type,
        source_clock_ts=source_clock_ts,
        qc_algorithm_version=qc_algorithm_version,
        payload=payload,
    )


def parse_jsonl_batch(text: str) -> list[QCEvent | MalformedRecord]:
    """Parse a JSONL blob into a mixed list of valid + malformed records.

    Records are returned in input order. Each line is parsed
    independently; one bad line does NOT short-circuit the rest. This
    matches the spec's data-quality model: malformed records flow to
    ``data_quality_events`` (quarantine), valid records flow to
    ``audit_log``, and the cursor advances past both.

    Empty / whitespace-only lines are silently skipped (NOT returned as
    MalformedRecord); JSONL by convention treats blank lines as
    separators.
    """
    out: list[QCEvent | MalformedRecord] = []
    for idx, raw_line in enumerate(text.splitlines()):
        if not raw_line.strip():
            continue
        try:
            out.append(parse_jsonl_record(raw_line))
        except MalformedPayloadError as exc:
            out.append(
                MalformedRecord(
                    raw_line=raw_line,
                    error=str(exc),
                    line_index=idx,
                )
            )
    return out


__all__ = [
    "REQUIRED_KEYS",
    "MalformedPayloadError",
    "MalformedRecord",
    "QCEvent",
    "parse_jsonl_batch",
    "parse_jsonl_record",
]
