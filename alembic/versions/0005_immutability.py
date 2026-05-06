"""Immutability mechanisms: audit_log triggers + attribution expected_* lock.

Implements backend-spec §2.10.2 (audit_log append-only enforcement) and §3.7
(attribution.expected_* columns immutable post-emit).

The spec's §2.10.2 also calls for `REVOKE TRUNCATE ON audit_log FROM PUBLIC,
app_service, app_owner; GRANT TRUNCATE ... TO dba_breakglass`. The role-named
REVOKE/GRANT statements move to 0006 (where roles are first created); this
migration handles only the trigger surfaces and the REVOKE FROM PUBLIC. See
Docs/decisions-log.md 2026-05-05 Day 3 entry for the full reasoning.

Revision ID: 0005_immutability
Revises: 0004_ops_tables
Create Date: 2026-05-05 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_immutability"
down_revision: str | None = "0004_ops_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # audit_log: BEFORE UPDATE OR DELETE → raise (spec §2.10.2)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_immutability_trigger()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append-only; UPDATE/DELETE forbidden (TG_OP=%)',
                TG_OP;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_immutability
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutability_trigger()
        """
    )

    # ------------------------------------------------------------------
    # audit_log: TRUNCATE blocker (spec §2.10.2 — corrected pattern)
    #
    # The spec specifies an EVENT TRIGGER on `ddl_command_start` filtered by
    # `TAG IN ('TRUNCATE TABLE')`. Postgres does NOT actually support
    # `TRUNCATE TABLE` as a tag for `ddl_command_start` — empirically:
    #     ERROR: event triggers are not supported for TRUNCATE TABLE
    # See https://www.postgresql.org/docs/current/event-trigger-matrix.html
    # for the supported tag set.
    #
    # The correct mechanism is a statement-level BEFORE TRUNCATE trigger ON
    # the table (and each partition). That fires for direct TRUNCATEs on the
    # parent partitioned table; for TRUNCATEs targeting a specific partition
    # (e.g. `TRUNCATE audit_log_y2026`), the parent's trigger does NOT fire,
    # so we attach the same trigger to each partition explicitly.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION block_audit_truncate()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'TRUNCATE forbidden on audit_log';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT
        EXECUTE FUNCTION block_audit_truncate()
        """
    )
    for year in (2026, 2027, 2028, 2029, 2030, 2031):
        op.execute(
            f"""
            CREATE TRIGGER audit_log_y{year}_no_truncate
            BEFORE TRUNCATE ON audit_log_y{year}
            FOR EACH STATEMENT
            EXECUTE FUNCTION block_audit_truncate()
            """
        )

    # ------------------------------------------------------------------
    # attribution: BEFORE UPDATE → expected_* columns frozen (spec §3.7)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION attribution_immutability()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.expected_entry_price IS DISTINCT FROM OLD.expected_entry_price
               OR NEW.expected_exit_price IS DISTINCT FROM OLD.expected_exit_price
               OR NEW.expected_pnl_usd IS DISTINCT FROM OLD.expected_pnl_usd
               OR NEW.expected_slippage_bps IS DISTINCT FROM OLD.expected_slippage_bps
               OR NEW.expected_holding_days IS DISTINCT FROM OLD.expected_holding_days
               OR NEW.expected_at_utc IS DISTINCT FROM OLD.expected_at_utc
               OR NEW.trade_id IS DISTINCT FROM OLD.trade_id THEN
                RAISE EXCEPTION
                    'expected_* columns are immutable post-emit';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER attribution_immutability_trigger
        BEFORE UPDATE ON attribution
        FOR EACH ROW EXECUTE FUNCTION attribution_immutability()
        """
    )

    # ------------------------------------------------------------------
    # Default-deny TRUNCATE for PUBLIC. Per-role REVOKEs (app_service,
    # app_owner) and the dba_breakglass GRANT land in 0006 where the
    # roles are first created.
    # ------------------------------------------------------------------
    op.execute("REVOKE TRUNCATE ON audit_log FROM PUBLIC")


def downgrade() -> None:
    op.execute("GRANT TRUNCATE ON audit_log TO PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS attribution_immutability_trigger ON attribution")
    op.execute("DROP FUNCTION IF EXISTS attribution_immutability()")
    for year in (2026, 2027, 2028, 2029, 2030, 2031):
        op.execute(f"DROP TRIGGER IF EXISTS audit_log_y{year}_no_truncate ON audit_log_y{year}")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS block_audit_truncate()")
    op.execute("DROP TRIGGER IF EXISTS audit_log_immutability ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutability_trigger()")
