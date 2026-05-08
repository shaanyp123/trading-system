"""Unit tests for ``services/qc_adapter/payloads.py``.

Schema validation per backend-spec §4.5.1:
- sequence_no: int >= 0 (BIGINT)
- event_type: non-empty string (taxonomy NOT validated here)
- source_clock_ts: RFC 3339 / ISO 8601 with tz offset
- qc_algorithm_version: non-empty string
- payload: JSON object
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from services.qc_adapter.payloads import (
    MalformedPayloadError,
    MalformedRecord,
    QCEvent,
    parse_jsonl_batch,
    parse_jsonl_record,
)


def _good_line(seq_no: int = 1042) -> str:
    return (
        '{"sequence_no":' + str(seq_no) + ","
        '"event_type":"signal_emitted",'
        '"source_clock_ts":"2026-05-04T17:30:01.234+00:00",'
        '"qc_algorithm_version":"v1-trend-following-9d2f7a1c",'
        '"payload":{"market":"MES","direction":"long"}}'
    )


class TestHappyPath:
    def test_parses_canonical_signal_emitted_payload(self) -> None:
        ev = parse_jsonl_record(_good_line(1042))
        assert isinstance(ev, QCEvent)
        assert ev.sequence_no == 1042
        assert ev.event_type == "signal_emitted"
        assert ev.qc_algorithm_version == "v1-trend-following-9d2f7a1c"
        assert ev.source_clock_ts.tzinfo is not None
        assert ev.payload == {"market": "MES", "direction": "long"}

    def test_accepts_trailing_z_timestamp(self) -> None:
        line = _good_line().replace("+00:00", "Z")
        ev = parse_jsonl_record(line)
        assert ev.source_clock_ts.tzinfo is not None
        assert ev.source_clock_ts == datetime(2026, 5, 4, 17, 30, 1, 234000, tzinfo=UTC)

    def test_accepts_non_utc_offset(self) -> None:
        line = _good_line().replace(
            "2026-05-04T17:30:01.234+00:00", "2026-05-04T13:30:01.234-04:00"
        )
        ev = parse_jsonl_record(line)
        assert ev.source_clock_ts == datetime(
            2026,
            5,
            4,
            13,
            30,
            1,
            234000,
            tzinfo=timezone(__import__("datetime").timedelta(hours=-4)),
        )

    def test_sequence_no_zero_is_valid(self) -> None:
        ev = parse_jsonl_record(_good_line(0))
        assert ev.sequence_no == 0


class TestStructuralRejection:
    def test_empty_line_raises(self) -> None:
        with pytest.raises(MalformedPayloadError, match="empty"):
            parse_jsonl_record("")

    def test_whitespace_only_line_raises(self) -> None:
        with pytest.raises(MalformedPayloadError, match="empty"):
            parse_jsonl_record("   \t  ")

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(MalformedPayloadError, match="invalid JSON"):
            parse_jsonl_record('{"sequence_no": 1, ')  # truncated

    def test_top_level_array_raises(self) -> None:
        with pytest.raises(MalformedPayloadError, match="top-level must be a JSON object"):
            parse_jsonl_record("[1, 2, 3]")

    def test_missing_sequence_no_raises(self) -> None:
        line = _good_line().replace('"sequence_no":1042,', "")
        with pytest.raises(MalformedPayloadError, match="missing required key"):
            parse_jsonl_record(line)

    def test_missing_event_type_raises(self) -> None:
        line = _good_line().replace('"event_type":"signal_emitted",', "")
        with pytest.raises(MalformedPayloadError, match="missing required key"):
            parse_jsonl_record(line)

    def test_missing_payload_raises(self) -> None:
        line = _good_line().replace(',"payload":{"market":"MES","direction":"long"}', "")
        with pytest.raises(MalformedPayloadError, match="missing required key"):
            parse_jsonl_record(line)


class TestFieldTypeRejection:
    def test_sequence_no_string_raises(self) -> None:
        line = _good_line().replace('"sequence_no":1042', '"sequence_no":"1042"')
        with pytest.raises(MalformedPayloadError, match="sequence_no must be int"):
            parse_jsonl_record(line)

    def test_sequence_no_negative_raises(self) -> None:
        line = _good_line().replace('"sequence_no":1042', '"sequence_no":-1')
        with pytest.raises(MalformedPayloadError, match="sequence_no must be >= 0"):
            parse_jsonl_record(line)

    def test_sequence_no_bool_true_rejected(self) -> None:
        # Python's isinstance(True, int) is True; the parser must reject
        # explicitly so {"sequence_no": true} doesn't silently parse as 1.
        line = _good_line().replace('"sequence_no":1042', '"sequence_no":true')
        with pytest.raises(MalformedPayloadError, match="sequence_no must be int"):
            parse_jsonl_record(line)

    def test_event_type_empty_string_raises(self) -> None:
        line = _good_line().replace('"event_type":"signal_emitted"', '"event_type":""')
        with pytest.raises(MalformedPayloadError, match="event_type must be a non-empty"):
            parse_jsonl_record(line)

    def test_event_type_non_string_raises(self) -> None:
        line = _good_line().replace('"event_type":"signal_emitted"', '"event_type":42')
        with pytest.raises(MalformedPayloadError, match="event_type must be a non-empty"):
            parse_jsonl_record(line)

    def test_qc_algorithm_version_empty_raises(self) -> None:
        line = _good_line().replace(
            '"qc_algorithm_version":"v1-trend-following-9d2f7a1c"',
            '"qc_algorithm_version":""',
        )
        with pytest.raises(MalformedPayloadError, match="qc_algorithm_version"):
            parse_jsonl_record(line)

    def test_source_clock_ts_naive_timestamp_raises(self) -> None:
        # No tz offset -> A06 violation.
        line = _good_line().replace(
            '"source_clock_ts":"2026-05-04T17:30:01.234+00:00"',
            '"source_clock_ts":"2026-05-04T17:30:01.234"',
        )
        with pytest.raises(MalformedPayloadError, match="must include a timezone"):
            parse_jsonl_record(line)

    def test_source_clock_ts_unparseable_raises(self) -> None:
        line = _good_line().replace(
            '"source_clock_ts":"2026-05-04T17:30:01.234+00:00"',
            '"source_clock_ts":"not-a-timestamp"',
        )
        with pytest.raises(MalformedPayloadError, match="not RFC 3339"):
            parse_jsonl_record(line)

    def test_source_clock_ts_non_string_raises(self) -> None:
        line = _good_line().replace(
            '"source_clock_ts":"2026-05-04T17:30:01.234+00:00"',
            '"source_clock_ts":1234567890',
        )
        with pytest.raises(MalformedPayloadError, match="must be a string"):
            parse_jsonl_record(line)

    def test_payload_array_raises(self) -> None:
        line = _good_line().replace(
            '"payload":{"market":"MES","direction":"long"}',
            '"payload":["a","b"]',
        )
        with pytest.raises(MalformedPayloadError, match="payload must be a JSON object"):
            parse_jsonl_record(line)


class TestNoTaxonomyValidation:
    """Per module docstring: parsing layer does NOT validate event_type
    against backend-spec §3.30 — that's the audit writer's job at the
    boundary. An unknown event_type should parse cleanly here so the
    caller can route it to data_quality_quarantine instead of treating
    it as a parse error.
    """

    def test_unknown_event_type_parses_cleanly(self) -> None:
        line = _good_line().replace(
            '"event_type":"signal_emitted"',
            '"event_type":"some_future_event_type_not_in_taxonomy"',
        )
        ev = parse_jsonl_record(line)
        assert ev.event_type == "some_future_event_type_not_in_taxonomy"


class TestBatchParsing:
    def test_empty_input_returns_empty_list(self) -> None:
        assert parse_jsonl_batch("") == []

    def test_blank_lines_are_silently_skipped(self) -> None:
        out = parse_jsonl_batch("\n\n\n")
        assert out == []

    def test_three_good_lines_parse_in_order(self) -> None:
        text = "\n".join(_good_line(i) for i in (10, 11, 12))
        out = parse_jsonl_batch(text)
        assert len(out) == 3
        assert all(isinstance(ev, QCEvent) for ev in out)
        assert [ev.sequence_no for ev in out] == [10, 11, 12]  # type: ignore[union-attr]

    def test_one_bad_line_quarantines_only_that_line(self) -> None:
        text = "\n".join([_good_line(10), "{not json", _good_line(12)])
        out = parse_jsonl_batch(text)
        assert len(out) == 3

        assert isinstance(out[0], QCEvent)
        assert out[0].sequence_no == 10

        assert isinstance(out[1], MalformedRecord)
        assert out[1].line_index == 1
        assert "invalid JSON" in out[1].error
        assert out[1].raw_line == "{not json"

        assert isinstance(out[2], QCEvent)
        assert out[2].sequence_no == 12

    def test_blank_line_in_middle_does_not_consume_an_index_for_malformed(self) -> None:
        # Blank line at index 1 is silently dropped; the malformed line is
        # at index 2 in the original split.
        text = _good_line(10) + "\n\n{bad\n" + _good_line(12)
        out = parse_jsonl_batch(text)
        assert len(out) == 3
        assert isinstance(out[0], QCEvent)
        assert isinstance(out[1], MalformedRecord)
        assert out[1].line_index == 2
        assert isinstance(out[2], QCEvent)
