"""audit_log + uuid_generate_v7() helper + yearly partitions 2026-2031.

Implements backend-spec §3.2 (audit_log schema, hash-chained, partitioned by
ingest_clock_ts year). Triggers + REVOKEs land in 0005; roles in 0006.

The `uuid_generate_v7()` function is a pure-PL/pgSQL implementation built on
pgcrypto's `gen_random_bytes()` and `clock_timestamp()`. Postgres 16 ships
`gen_random_uuid()` (v4) in core but not v7; rather than add a third-party
extension (pg_uuidv7) to the runtime image we keep the function self-contained
in this migration. Behavior is RFC 9562 §5.7-compliant: 48-bit unix-ms
timestamp, 4-bit version (7), 2-bit variant (10), 74 random bits.

Revision ID: 0001_audit_log
Revises:
Create Date: 2026-05-05 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_audit_log"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PARTITION_YEARS: tuple[int, ...] = (2026, 2027, 2028, 2029, 2030, 2031)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION uuid_generate_v7()
        RETURNS uuid
        LANGUAGE plpgsql
        VOLATILE
        AS $$
        DECLARE
            unix_ts_ms bytea;
            uuid_bytes bytea;
        BEGIN
            unix_ts_ms := substring(
                int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint)
                FROM 3
            );
            uuid_bytes := unix_ts_ms || gen_random_bytes(10);
            -- Set version (bits 48-51) to 7
            uuid_bytes := set_byte(
                uuid_bytes, 6,
                ((b'01110000'::int) | (get_byte(uuid_bytes, 6) & 15))
            );
            -- Set variant (bits 64-65) to 10 (RFC 9562)
            uuid_bytes := set_byte(
                uuid_bytes, 8,
                ((b'10000000'::int) | (get_byte(uuid_bytes, 8) & 63))
            );
            RETURN encode(uuid_bytes, 'hex')::uuid;
        END
        $$
        """
    )

    op.execute(
        """
        CREATE TABLE audit_log (
            event_uuid UUID NOT NULL DEFAULT uuid_generate_v7(),
            sequence_no BIGSERIAL NOT NULL,
            event_type TEXT NOT NULL,
            account_id UUID NOT NULL,
            env TEXT NOT NULL CHECK (env IN ('paper','live-small','live-scale')),
            phase_at_emit SMALLINT NOT NULL CHECK (phase_at_emit IN (0,1,2,3)),
            source_clock_ts TIMESTAMPTZ NOT NULL,
            ingest_clock_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            monotonic_ns BIGINT,
            prev_hash BYTEA NOT NULL CHECK (octet_length(prev_hash) = 32),
            record_hash BYTEA NOT NULL CHECK (octet_length(record_hash) = 32),
            payload_jcs BYTEA NOT NULL,
            repaired_for_sequence_no BIGINT,
            repaired_for_event_timestamp TIMESTAMPTZ,
            PRIMARY KEY (sequence_no, ingest_clock_ts)
        ) PARTITION BY RANGE (ingest_clock_ts)
        """
    )

    for year in _PARTITION_YEARS:
        op.execute(
            f"""
            CREATE TABLE audit_log_y{year} PARTITION OF audit_log
            FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
            """
        )

    op.execute(
        "CREATE INDEX audit_log_event_type_idx ON audit_log(event_type, ingest_clock_ts DESC)"
    )
    op.execute("CREATE INDEX audit_log_event_uuid_idx ON audit_log(event_uuid)")
    # Spec §3.2 calls for `CREATE UNIQUE INDEX ... ON audit_log(sequence_no)`,
    # but Postgres rejects a UNIQUE index on a partitioned table that omits
    # the partition key (`ingest_clock_ts`). Global sequence_no uniqueness is
    # already guaranteed by the BIGSERIAL sequence (atomic, monotonic, never
    # reused across partitions). Index is kept non-unique for lookup speed.
    # See Docs/decisions-log.md 2026-05-05 Day 3 entry on this deviation.
    op.execute("CREATE INDEX audit_log_sequence_no_idx ON audit_log(sequence_no)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS audit_log_sequence_no_idx")
    op.execute("DROP INDEX IF EXISTS audit_log_event_uuid_idx")
    op.execute("DROP INDEX IF EXISTS audit_log_event_type_idx")
    for year in _PARTITION_YEARS:
        op.execute(f"DROP TABLE IF EXISTS audit_log_y{year}")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7()")
    # pgcrypto extension intentionally NOT dropped on downgrade — it may be
    # required by other components that share the database.
