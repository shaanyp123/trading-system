"""Unit tests for ``services/qc_adapter/cursor.py``.

Cursor advance semantics per backend-spec §3.19:
- Successful poll: sequence_no advances (or stays put on empty batch),
  timestamp updates, bytes_consumed accumulates, consecutive_failures resets.
- Failed poll: only consecutive_failures changes; everything else stays put.
- Sequence_no monotonicity: backend-spec §2.10.1 — going backward is a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.qc_adapter.cursor import (
    POLL_CADENCE_SECONDS,
    CursorAdvance,
    CursorDirectory,
    CursorRow,
    initial_cursor_rows,
    plan_cursor_advance_failure,
    plan_cursor_advance_success,
)

_T0 = datetime(2026, 5, 8, 21, 30, 0, tzinfo=UTC)


def _row(
    *,
    directory: CursorDirectory = CursorDirectory.EVENTS,
    seq_no: int = 100,
    bytes_consumed: int = 1024,
    consecutive_failures: int = 0,
) -> CursorRow:
    return CursorRow(
        directory_path=directory,
        last_consumed_sequence_no=seq_no,
        last_consumed_at_utc=_T0,
        bytes_consumed=bytes_consumed,
        consecutive_failures=consecutive_failures,
    )


class TestCanonicalDirectories:
    """Spec §3.19 locks the three directories. Any new entry requires an
    alembic migration + this enum updated; both are forbidden-whitelist
    paths so the change carries `risk-review-approved`.
    """

    def test_three_canonical_directories(self) -> None:
        assert {d.value for d in CursorDirectory} == {
            "/events/",
            "/state/portfolio.json",
            "/instruction_acks/",
        }

    def test_poll_cadence_matches_spec(self) -> None:
        # backend-spec §1.4 (line 105) + §2.4 (60s portfolio cadence)
        assert POLL_CADENCE_SECONDS[CursorDirectory.EVENTS] == 60
        assert POLL_CADENCE_SECONDS[CursorDirectory.STATE_PORTFOLIO] == 60
        assert POLL_CADENCE_SECONDS[CursorDirectory.INSTRUCTION_ACKS] == 5


class TestSuccessfulPoll:
    def test_advances_sequence_and_timestamp_resets_counter(self) -> None:
        cursor = _row(seq_no=100, consecutive_failures=2, bytes_consumed=1024)
        now = _T0 + timedelta(minutes=5)

        plan = plan_cursor_advance_success(
            cursor=cursor,
            max_observed_sequence_no=105,
            bytes_in_batch=512,
            now_utc=now,
        )

        assert isinstance(plan, CursorAdvance)
        assert plan.directory_path == CursorDirectory.EVENTS
        assert plan.last_consumed_sequence_no == 105
        assert plan.last_consumed_at_utc == now
        assert plan.bytes_consumed == 1024 + 512
        assert plan.consecutive_failures == 0

    def test_empty_batch_advances_timestamp_only(self) -> None:
        cursor = _row(seq_no=100, consecutive_failures=3)
        now = _T0 + timedelta(minutes=5)

        plan = plan_cursor_advance_success(
            cursor=cursor,
            max_observed_sequence_no=100,  # caller passes cursor's seq for empty batch
            bytes_in_batch=0,
            now_utc=now,
        )

        assert plan.last_consumed_sequence_no == 100  # unchanged
        assert plan.last_consumed_at_utc == now
        assert plan.bytes_consumed == cursor.bytes_consumed
        assert plan.consecutive_failures == 0  # successful empty poll resets

    def test_sequence_regression_raises(self) -> None:
        cursor = _row(seq_no=100)
        with pytest.raises(ValueError, match="sequence_no regressed"):
            plan_cursor_advance_success(
                cursor=cursor,
                max_observed_sequence_no=99,  # backward
                bytes_in_batch=10,
                now_utc=_T0,
            )

    def test_negative_byte_count_raises(self) -> None:
        cursor = _row()
        with pytest.raises(ValueError, match="bytes_in_batch must be >= 0"):
            plan_cursor_advance_success(
                cursor=cursor,
                max_observed_sequence_no=cursor.last_consumed_sequence_no,
                bytes_in_batch=-1,
                now_utc=_T0,
            )


class TestFailedPoll:
    def test_increments_failure_counter_only(self) -> None:
        cursor = _row(seq_no=100, bytes_consumed=1024, consecutive_failures=2)
        plan = plan_cursor_advance_failure(cursor=cursor)

        assert plan.directory_path == CursorDirectory.EVENTS
        assert plan.last_consumed_sequence_no == 100  # unchanged
        assert plan.last_consumed_at_utc == _T0  # unchanged
        assert plan.bytes_consumed == 1024  # unchanged
        assert plan.consecutive_failures == 3  # +1

    def test_failure_from_zero_counter(self) -> None:
        cursor = _row(consecutive_failures=0)
        plan = plan_cursor_advance_failure(cursor=cursor)
        assert plan.consecutive_failures == 1


class TestInitialRows:
    def test_returns_three_rows_one_per_directory(self) -> None:
        rows = initial_cursor_rows(now_utc=_T0)
        assert len(rows) == 3
        assert {r.directory_path for r in rows} == set(CursorDirectory)

    def test_initial_rows_match_spec_seed(self) -> None:
        rows = initial_cursor_rows(now_utc=_T0)
        for row in rows:
            assert row.last_consumed_sequence_no == 0
            assert row.bytes_consumed == 0
            assert row.consecutive_failures == 0
            assert row.last_consumed_at_utc == _T0
