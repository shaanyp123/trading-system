"""Unit tests for ``services/qc_adapter/poll.py``.

Plan-then-apply contract per backend-spec §2.10.1 + §3.19 + §4.5.1:
- HTTP failure  -> failure cursor advance, no events, no malformed.
- HTTP empty    -> success cursor advance (timestamp only), no events.
- HTTP records  -> success cursor advance (max seq), QCEvents, MalformedRecords.
- ``now_utc`` must be timezone-aware (anti-pattern A06).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.qc_adapter.cursor import (
    POLL_CADENCE_SECONDS,
    CursorDirectory,
    CursorRow,
)
from services.qc_adapter.payloads import MalformedRecord, QCEvent
from services.qc_adapter.poll import IngestPlan, plan_ingest_batch

_T0 = datetime(2026, 5, 8, 21, 30, 0, tzinfo=UTC)


def _events_cursor(seq_no: int = 100, failures: int = 0) -> CursorRow:
    return CursorRow(
        directory_path=CursorDirectory.EVENTS,
        last_consumed_sequence_no=seq_no,
        last_consumed_at_utc=_T0,
        bytes_consumed=1024,
        consecutive_failures=failures,
    )


def _good_line(seq_no: int) -> str:
    return (
        '{"sequence_no":' + str(seq_no) + ","
        '"event_type":"signal_emitted",'
        '"source_clock_ts":"2026-05-04T17:30:01.234+00:00",'
        '"qc_algorithm_version":"v1-trend-following-9d2f7a1c",'
        '"payload":{"market":"MES","direction":"long"}}'
    )


class TestNaiveTimestampRejected:
    def test_naive_now_utc_raises(self) -> None:
        with pytest.raises(ValueError, match="A06"):
            plan_ingest_batch(
                cursor=_events_cursor(),
                response_body="",
                now_utc=datetime(2026, 5, 8, 21, 30, 0),  # naive
            )


class TestHTTPFailure:
    def test_response_body_none_increments_failure_counter(self) -> None:
        cursor = _events_cursor(seq_no=100, failures=2)
        plan = plan_ingest_batch(
            cursor=cursor,
            response_body=None,
            now_utc=_T0 + timedelta(minutes=5),
        )

        assert isinstance(plan, IngestPlan)
        assert plan.audit_events == ()
        assert plan.malformed_records == ()
        assert plan.cursor_advance.consecutive_failures == 3
        assert plan.cursor_advance.last_consumed_sequence_no == 100  # unchanged
        assert plan.cursor_advance.last_consumed_at_utc == _T0  # unchanged
        assert plan.next_poll_in_seconds == POLL_CADENCE_SECONDS[CursorDirectory.EVENTS]


class TestHTTPSuccessEmptyBody:
    def test_empty_string_body_resets_failure_counter(self) -> None:
        cursor = _events_cursor(seq_no=100, failures=4)
        now = _T0 + timedelta(minutes=5)

        plan = plan_ingest_batch(
            cursor=cursor,
            response_body="",
            now_utc=now,
        )

        assert plan.audit_events == ()
        assert plan.malformed_records == ()
        assert plan.cursor_advance.consecutive_failures == 0
        assert plan.cursor_advance.last_consumed_sequence_no == 100  # no advance
        assert plan.cursor_advance.last_consumed_at_utc == now  # timestamp moves


class TestHTTPSuccessWithRecords:
    def test_three_good_records_advance_to_max_sequence(self) -> None:
        cursor = _events_cursor(seq_no=100, failures=1)
        body = "\n".join(_good_line(s) for s in (101, 102, 103))
        now = _T0 + timedelta(minutes=1)

        plan = plan_ingest_batch(cursor=cursor, response_body=body, now_utc=now)

        assert len(plan.audit_events) == 3
        assert all(isinstance(ev, QCEvent) for ev in plan.audit_events)
        assert [ev.sequence_no for ev in plan.audit_events] == [101, 102, 103]
        assert plan.malformed_records == ()
        assert plan.cursor_advance.last_consumed_sequence_no == 103
        assert plan.cursor_advance.last_consumed_at_utc == now
        assert plan.cursor_advance.consecutive_failures == 0
        # bytes_consumed accumulates the body length
        assert plan.cursor_advance.bytes_consumed == cursor.bytes_consumed + len(
            body.encode("utf-8")
        )

    def test_mixed_good_and_malformed_quarantines_malformed_only(self) -> None:
        cursor = _events_cursor(seq_no=100)
        body = "\n".join([_good_line(101), "{not json", _good_line(103)])

        plan = plan_ingest_batch(
            cursor=cursor, response_body=body, now_utc=_T0 + timedelta(seconds=30)
        )

        # Two QCEvents survive parsing; the bad line is quarantined.
        assert len(plan.audit_events) == 2
        assert [ev.sequence_no for ev in plan.audit_events] == [101, 103]

        assert len(plan.malformed_records) == 1
        assert isinstance(plan.malformed_records[0], MalformedRecord)
        assert plan.malformed_records[0].line_index == 1

        # Cursor still advances to max(101, 103) = 103, even though one
        # record in the middle was malformed. The rationale is that the
        # bad record HAS been seen; the cursor must not re-deliver it on
        # the next poll. data_quality_events captures the quarantine.
        assert plan.cursor_advance.last_consumed_sequence_no == 103

    def test_all_malformed_does_not_advance_sequence(self) -> None:
        cursor = _events_cursor(seq_no=100)
        body = "{not json\nalso garbage\n}}"

        plan = plan_ingest_batch(
            cursor=cursor, response_body=body, now_utc=_T0 + timedelta(seconds=30)
        )

        assert plan.audit_events == ()
        assert len(plan.malformed_records) == 3
        # No good records means no max sequence_no observed; cursor stays.
        assert plan.cursor_advance.last_consumed_sequence_no == 100
        # But the poll itself succeeded (HTTP 2xx + parseable line iter),
        # so the failure counter still resets.
        assert plan.cursor_advance.consecutive_failures == 0


class TestPerDirectoryCadence:
    @pytest.mark.parametrize(
        ("directory", "expected_cadence_seconds"),
        [
            (CursorDirectory.EVENTS, 60),
            (CursorDirectory.STATE_PORTFOLIO, 60),
            (CursorDirectory.INSTRUCTION_ACKS, 5),
        ],
    )
    def test_next_poll_in_seconds_matches_directory(
        self, directory: CursorDirectory, expected_cadence_seconds: int
    ) -> None:
        cursor = CursorRow(
            directory_path=directory,
            last_consumed_sequence_no=0,
            last_consumed_at_utc=_T0,
            bytes_consumed=0,
            consecutive_failures=0,
        )
        plan = plan_ingest_batch(cursor=cursor, response_body="", now_utc=_T0)
        assert plan.next_poll_in_seconds == expected_cadence_seconds
