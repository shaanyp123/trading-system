"""Golden parity test for the QC → audit-log hash chain.

Implementation of the dev-guide §6.5 canonical pattern + the
implementation-guide §3 Week 4 Mon goal: byte-for-byte reproducibility of
the audit ``record_hash`` for five representative QC ObjectStore session
events.

What this suite asserts
-----------------------

1. **Per-event determinism.** For each of the 5 representative event types
   (``signal_emitted``, ``order_filled``, ``reconciliation_check_passed``,
   ``kill_switch_triggered``, ``system_stopped``) the ``record_hash``
   computed from a fresh chain (``prev_hash = GENESIS_HASH``) matches a
   hex string baked into this module. A drift in either
   :func:`services.audit.chain.jcs_serialize` OR
   :func:`services.audit.chain.compute_record_hash` trips the assertion
   even when the fixture file is correctly read.

2. **Chain composition.** Feeding the 5 fixtures sequentially walks the
   chain forward — each row's ``prev_hash`` is the previous row's
   ``record_hash`` and the final tail hash matches a baked-in value.

3. **QC-adapter parser identity.** Round-tripping each fixture through
   :func:`services.qc_adapter.payloads.parse_jsonl_record` and
   re-canonicalizing the parsed ``payload`` must produce JCS bytes
   byte-identical to canonicalizing the original fixture's ``payload``
   directly. The parser is a payload-identity transform; if it ever
   coerces, reorders, or mutates the payload dict it would break the
   hash chain.

4. **Modulo three mutable fields.** The QC wire payload (the inner
   ``payload`` sub-field of the §4.5.1 envelope) MUST NOT contain
   ``ingest_clock_ts``, ``ingest_uuid``, or ``sequence_no``. Those three
   fields are added by :mod:`services.audit.writer` at INSERT time
   (``ingest_clock_ts`` = server NOW, ``ingest_uuid`` = UUIDv7, audit
   ``sequence_no`` = Postgres-assigned). If they ever leak into QC's wire
   schema, the hash chain becomes non-deterministic across runs and the
   "modulo three mutable fields" parity guarantee from
   ``implementation-guide.md`` §3 Week 4 Mon is violated.

   Note the careful distinction: the §4.5.1 wire envelope has its OWN
   top-level ``sequence_no`` (QC-side, per-directory monotonic) — that's
   allowed and required. The forbidden ``sequence_no`` is the audit_log
   row's own column, which must never be in the JCS-canonicalized payload.

A22 / scope notes
-----------------

This is pure-Python golden data; A22 ("DO NOT emit audit events from
within tests unless you are specifically testing the audit chain") is
satisfied because no audit row is INSERTed. The whole suite runs against
the in-tree primitives :func:`jcs_serialize` and
:func:`compute_record_hash` without Postgres. Database-bound tests for
the writer live in ``tests/integration/test_audit_writer.py``.

A27 ("DO NOT introduce a service file that talks to a third-party
platform without a smoke test or runbook") does not bind: this module
exercises only in-tree pure-Python primitives. The QC ObjectStore
HTTP-fetch surface is not part of this PR; A27 transfers to the Week 4
PR that introduces ``services/qc_adapter/poll.py``'s HTTP fetcher.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from services.audit.chain import (
    GENESIS_HASH,
    HASH_LENGTH_BYTES,
    compute_record_hash,
    jcs_serialize,
)
from services.qc_adapter.payloads import (
    QCEvent,
    parse_jsonl_record,
)

#: Where the 5 representative QC §4.5.1 wire envelopes live. Each file is a
#: single JSON object: ``{sequence_no, event_type, source_clock_ts,
#: qc_algorithm_version, payload}``.
FIXTURE_DIR: Final[Path] = Path(__file__).parent / "fixtures" / "qc_events"

#: Ordered tuple of fixture filenames. The numeric prefix forces an
#: unambiguous chain order for :class:`TestChainLinksForward` and for the
#: per-event tests' shared list.
ORDERED_FIXTURE_FILES: Final[tuple[str, ...]] = (
    "01_signal_emitted.json",
    "02_order_filled.json",
    "03_reconciliation_check_passed.json",
    "04_kill_switch_triggered.json",
    "05_system_stopped.json",
)

#: Hex-encoded ``record_hash`` for each fixture, computed against
#: ``prev_hash = GENESIS_HASH`` (i.e. as if each fixture were the very
#: first row of a fresh chain). Baked into the test so that a drift in
#: ``jcs_serialize`` OR ``compute_record_hash`` trips the assertion even
#: if the fixture file is correctly read.
#:
#: To regenerate after a deliberate fixture or chain-primitive change,
#: re-run::
#:
#:     python3 -c "
#:     import json
#:     from pathlib import Path
#:     from services.audit.chain import GENESIS_HASH, compute_record_hash, jcs_serialize
#:     for f in sorted(Path('tests/golden/fixtures/qc_events').glob('*.json')):
#:         env = json.loads(f.read_text())
#:         h = compute_record_hash(GENESIS_HASH, jcs_serialize(env['payload']))
#:         print(f'{f.name}: {h.hex()}')
#:     "
EXPECTED_RECORD_HASH_FROM_GENESIS_HEX: Final[dict[str, str]] = {
    "01_signal_emitted.json": ("16f2c41d49b0969c1240fbf788564b0c852377a2576977d43a6fea431c8e00b9"),
    "02_order_filled.json": ("8e3df087efcb8acb90c5903f466e971c72321c54792d7272cc9b12ced0ab3ddc"),
    "03_reconciliation_check_passed.json": (
        "37432fcebbd3ab7d21c5e0b0e75624986256bf75df2105e869bb9543a5c94cb8"
    ),
    "04_kill_switch_triggered.json": (
        "e83de5d964a8d129f42c4cf833dd38f5ad107a0144a32a7d6fa1d08259d62008"
    ),
    "05_system_stopped.json": ("b8c24ba4703802542aa59c4714f932e611ca9f9e9452582a1bf3f3c48f18539c"),
}

#: Hex of the chain tail after walking all 5 fixtures sequentially, each
#: fixture's ``prev_hash`` set to the previous fixture's ``record_hash``
#: (the first fixture's ``prev_hash`` is :data:`GENESIS_HASH`).
EXPECTED_CHAIN_TAIL_HEX: Final[str] = (
    "0c7a9a9c607e914a921ccc224c759fd72d4d0e707d729711f179f669066c4d1b"
)

#: Fields added by :mod:`services.audit.writer` at INSERT time. They MUST
#: NOT appear in QC's wire payload — see module docstring.
AUDIT_WRITER_ADDED_FIELDS: Final[frozenset[str]] = frozenset(
    {"ingest_clock_ts", "ingest_uuid", "sequence_no"}
)

pytestmark = pytest.mark.golden


def _load_fixture(name: str) -> dict[str, Any]:
    """Read and JSON-parse a fixture file.

    Returns the full §4.5.1 wire envelope as a dict; call sites usually
    index ``["payload"]`` to get the inner application payload.
    """
    path = FIXTURE_DIR / name
    return json.loads(path.read_text())


class TestRecordHashFromGenesis:
    """Per-fixture deterministic record_hash check.

    For each representative event type, ``compute_record_hash(GENESIS_HASH,
    jcs_serialize(envelope["payload"]))`` MUST produce a 32-byte hash whose
    hex matches the value baked into
    :data:`EXPECTED_RECORD_HASH_FROM_GENESIS_HEX`. This is the strongest
    guarantee in the suite — any drift in JCS canonicalization OR the
    SHA-256 record-hash math trips the assertion.
    """

    def test_signal_emitted_record_hash_deterministic(self) -> None:
        self._assert_matches_baked_hex("01_signal_emitted.json")

    def test_order_filled_record_hash_deterministic(self) -> None:
        self._assert_matches_baked_hex("02_order_filled.json")

    def test_reconciliation_check_passed_record_hash_deterministic(self) -> None:
        self._assert_matches_baked_hex("03_reconciliation_check_passed.json")

    def test_kill_switch_triggered_record_hash_deterministic(self) -> None:
        self._assert_matches_baked_hex("04_kill_switch_triggered.json")

    def test_system_stopped_record_hash_deterministic(self) -> None:
        self._assert_matches_baked_hex("05_system_stopped.json")

    def _assert_matches_baked_hex(self, name: str) -> None:
        envelope = _load_fixture(name)
        payload_jcs = jcs_serialize(envelope["payload"])
        record_hash = compute_record_hash(GENESIS_HASH, payload_jcs)
        assert len(record_hash) == HASH_LENGTH_BYTES
        assert record_hash.hex() == EXPECTED_RECORD_HASH_FROM_GENESIS_HEX[name], (
            f"{name}: record_hash drift detected.\n"
            f"  computed: {record_hash.hex()}\n"
            f"  expected: {EXPECTED_RECORD_HASH_FROM_GENESIS_HEX[name]}\n"
            f"  payload_jcs (len={len(payload_jcs)}): {payload_jcs!r}\n"
            f"If this drift is intentional (a deliberate change to the\n"
            f"fixture or to chain.jcs_serialize / chain.compute_record_hash),\n"
            f"regenerate the baked-in hex per this module's docstring."
        )


class TestChainLinksForward:
    """Sequential-chain composition.

    Walks the 5 fixtures in numeric order with ``prev_hash`` chained from
    the previous row's ``record_hash`` (first row uses
    :data:`GENESIS_HASH`). Asserts the final tail hash matches
    :data:`EXPECTED_CHAIN_TAIL_HEX` and every intermediate hash has the
    canonical 32-byte length.
    """

    def test_chain_walks_through_all_five_events_in_order(self) -> None:
        prev = GENESIS_HASH
        for name in ORDERED_FIXTURE_FILES:
            envelope = _load_fixture(name)
            payload_jcs = jcs_serialize(envelope["payload"])
            record_hash = compute_record_hash(prev, payload_jcs)
            assert len(record_hash) == HASH_LENGTH_BYTES
            prev = record_hash
        assert prev.hex() == EXPECTED_CHAIN_TAIL_HEX, (
            f"chain tail drift after walking 5 fixtures.\n"
            f"  computed tail: {prev.hex()}\n"
            f"  expected tail: {EXPECTED_CHAIN_TAIL_HEX}\n"
            f"If a per-fixture record_hash drifted, the per-fixture test in\n"
            f"TestRecordHashFromGenesis will pinpoint which one."
        )

    def test_first_link_consistent_with_genesis_per_event_test(self) -> None:
        # Sanity cross-check: the chain-walk's first record_hash must equal
        # the per-event-from-GENESIS hash for the first fixture. If they
        # ever differ, the chain-walk loop has a bug independent of the
        # primitives.
        first = ORDERED_FIXTURE_FILES[0]
        envelope = _load_fixture(first)
        record_hash = compute_record_hash(GENESIS_HASH, jcs_serialize(envelope["payload"]))
        assert record_hash.hex() == EXPECTED_RECORD_HASH_FROM_GENESIS_HEX[first]


class TestRoundTripViaQCAdapterParser:
    """Parser identity check.

    For each fixture: serialize as JSONL → parse via
    :func:`services.qc_adapter.payloads.parse_jsonl_record` →
    re-canonicalize the parsed ``payload`` via
    :func:`services.audit.chain.jcs_serialize` → assert byte-identical to
    JCS-canonicalizing the original fixture ``payload`` directly.

    This pins the contract that the parser is a payload-identity
    transform: JSON parse + re-extract MUST NOT change the resulting JCS
    bytes. If it ever does (key reordering, string coercion, integer →
    float, etc.) the audit ``record_hash`` would diverge between QC's
    side and the backend's side and the chain would corrupt silently.
    """

    def test_signal_emitted_round_trip(self) -> None:
        self._assert_round_trip("01_signal_emitted.json")

    def test_order_filled_round_trip(self) -> None:
        self._assert_round_trip("02_order_filled.json")

    def test_reconciliation_check_passed_round_trip(self) -> None:
        self._assert_round_trip("03_reconciliation_check_passed.json")

    def test_kill_switch_triggered_round_trip(self) -> None:
        self._assert_round_trip("04_kill_switch_triggered.json")

    def test_system_stopped_round_trip(self) -> None:
        self._assert_round_trip("05_system_stopped.json")

    def _assert_round_trip(self, name: str) -> None:
        envelope = _load_fixture(name)
        # Re-serialize to JSONL exactly as QC's ObjectStore would emit.
        # ``json.dumps`` produces a one-line string; the parser tolerates
        # surrounding whitespace via ``.strip()``.
        jsonl_line = json.dumps(envelope)
        parsed = parse_jsonl_record(jsonl_line)
        assert isinstance(parsed, QCEvent)
        # Wire-envelope-level fields preserved exactly:
        assert parsed.sequence_no == envelope["sequence_no"]
        assert parsed.event_type == envelope["event_type"]
        assert parsed.qc_algorithm_version == envelope["qc_algorithm_version"]
        # JCS-byte-identity on the inner payload:
        original_jcs = jcs_serialize(envelope["payload"])
        parsed_jcs = jcs_serialize(parsed.payload)
        assert original_jcs == parsed_jcs, (
            f"{name}: parser mutated payload — JCS bytes diverged.\n"
            f"  original_jcs (len={len(original_jcs)}): {original_jcs!r}\n"
            f"  parsed_jcs   (len={len(parsed_jcs)}):   {parsed_jcs!r}"
        )
        # And the resulting record_hash from GENESIS still matches:
        record_hash = compute_record_hash(GENESIS_HASH, parsed_jcs)
        assert record_hash.hex() == EXPECTED_RECORD_HASH_FROM_GENESIS_HEX[name]


class TestModuloThreeMutableFields:
    """Pin the "modulo three mutable fields" invariant from IG §3 Week 4 Mon.

    The audit writer (``services.audit.writer.append_audit_event``) adds
    three fields at INSERT time:

    * ``ingest_clock_ts`` — server-side ``datetime.now(UTC)`` at write.
    * ``ingest_uuid``     — UUIDv7 minted per row.
    * ``sequence_no``     — audit_log-side, Postgres-assigned monotonic
      counter. Distinct from QC's wire ``sequence_no`` (which IS allowed
      at the wire envelope's TOP level — that's a per-directory QC-side
      counter, not an audit_log column).

    None of these three may appear in QC's wire payload's ``payload``
    sub-field. If they ever did, two distinct write attempts (rebooting
    the QC algorithm, retrying after a partial failure) would produce
    DIFFERENT JCS bytes for what should be the SAME logical event,
    breaking the chain's deterministic-hash guarantee.
    """

    def test_no_writer_added_fields_in_any_payload(self) -> None:
        leaks: dict[str, set[str]] = {}
        for name in ORDERED_FIXTURE_FILES:
            envelope = _load_fixture(name)
            payload_keys = set(envelope["payload"].keys())
            leaked = payload_keys & AUDIT_WRITER_ADDED_FIELDS
            if leaked:
                leaks[name] = leaked
        assert not leaks, (
            f"audit-writer-added fields leaked into QC wire payload(s):\n"
            f"  {leaks!r}\n"
            f"These fields are minted at INSERT time by\n"
            f"services.audit.writer.append_audit_event and MUST NOT appear\n"
            f"in QC's wire payload — including them would break the\n"
            f"deterministic-hash guarantee from IG §3 Week 4 Mon."
        )

    def test_no_writer_added_fields_in_nested_payload_objects(self) -> None:
        # Defense in depth: also walk nested dicts. signal_emitted's
        # `sizing_trace` is a representative nested object; future fixtures
        # may add more. A regression that adds e.g. `ingest_uuid` to a
        # nested `audit_metadata` dict would still corrupt parity.
        leaks: dict[str, set[str]] = {}
        for name in ORDERED_FIXTURE_FILES:
            envelope = _load_fixture(name)
            leaked = _collect_keys_recursive(envelope["payload"]) & AUDIT_WRITER_ADDED_FIELDS
            if leaked:
                leaks[name] = leaked
        assert not leaks, f"audit-writer-added fields leaked into nested payload(s):\n  {leaks!r}"


class TestFixtureSchemaShape:
    """Sanity checks on the fixture files themselves.

    Cheap structural assertions so that a fixture corruption is caught at
    a single spot rather than as a cascade of opaque hash-mismatch
    errors. These are the same constraints
    :func:`services.qc_adapter.payloads.parse_jsonl_record` enforces at
    runtime; running them as static fixture asserts gives a clearer
    failure mode for fixture-author errors.
    """

    def test_every_fixture_has_canonical_5_top_level_keys(self) -> None:
        expected = {
            "sequence_no",
            "event_type",
            "source_clock_ts",
            "qc_algorithm_version",
            "payload",
        }
        for name in ORDERED_FIXTURE_FILES:
            envelope = _load_fixture(name)
            assert set(envelope.keys()) == expected, (
                f"{name}: top-level keys diverge from §4.5.1 wire envelope.\n"
                f"  got:      {sorted(envelope.keys())!r}\n"
                f"  expected: {sorted(expected)!r}"
            )

    def test_every_fixture_event_type_is_in_audit_taxonomy(self) -> None:
        # Imported lazily so the module can still load if event_types.py
        # ever moves; the assertion is the point.
        from services.audit.event_types import AuditEventType

        valid = {e.value for e in AuditEventType}
        for name in ORDERED_FIXTURE_FILES:
            envelope = _load_fixture(name)
            assert envelope["event_type"] in valid, (
                f"{name}: event_type {envelope['event_type']!r} not in "
                f"AuditEventType taxonomy (anti-pattern A04)."
            )

    def test_every_fixture_source_clock_ts_has_explicit_offset(self) -> None:
        # A06: timestamps stored UTC, always carry tz offset.
        for name in ORDERED_FIXTURE_FILES:
            envelope = _load_fixture(name)
            ts = envelope["source_clock_ts"]
            assert isinstance(ts, str)
            assert ts.endswith("+00:00") or ts.endswith("Z"), (
                f"{name}: source_clock_ts {ts!r} lacks explicit UTC offset (A06)."
            )

    def test_ordered_fixture_files_matches_filesystem_listing(self) -> None:
        # Catches "added a fixture file but forgot to add it to
        # ORDERED_FIXTURE_FILES" — that fixture would be silently skipped
        # by every test in this module otherwise.
        on_disk = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
        assert on_disk == list(ORDERED_FIXTURE_FILES), (
            f"fixture directory drift:\n"
            f"  on disk:                   {on_disk!r}\n"
            f"  ORDERED_FIXTURE_FILES:     {list(ORDERED_FIXTURE_FILES)!r}\n"
            f"Add new fixtures to ORDERED_FIXTURE_FILES + "
            f"EXPECTED_RECORD_HASH_FROM_GENESIS_HEX so they are exercised."
        )


def _collect_keys_recursive(value: Any) -> set[str]:
    """Recursively gather all dict keys at any depth in ``value``."""
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value.keys())
        for v in value.values():
            keys |= _collect_keys_recursive(v)
    elif isinstance(value, list):
        for v in value:
            keys |= _collect_keys_recursive(v)
    return keys
