"""Grant USAGE on audit_log_sequence_no_seq to app_service (DP-022 fix).

Day 25 carryover. Production smoke surfaced a latent grant gap:

    asyncpg.exceptions.InsufficientPrivilegeError:
    permission denied for sequence audit_log_sequence_no_seq

Root cause: ``alembic/versions/0006_roles.py`` granted INSERT/SELECT on the
``audit_log`` table to ``app_service`` but never granted USAGE/SELECT on the
backing BIGSERIAL sequence. The ``ALTER DEFAULT PRIVILEGES ... ON TABLES``
clause at 0006:88-90 only covers future TABLES — sequences need a separate
``ON SEQUENCES`` grant. Postgres' BIGSERIAL implementation creates an
implicit sequence that DEFAULT requires USAGE on; without it, INSERTs from
``app_service`` fail with SQLSTATE 42501.

Why this stayed latent: every prior audit-writer integration test
(``tests/integration/test_audit_writer.py``) binds the session to the
testcontainer's SUPERUSER (the default postgres user). SUPERUSER has
implicit permissions on every object, so the grant gap never surfaced
in CI. Day-25's PR was the first time anything wrote to ``audit_log``
as ``app_service`` in production — which exposed this immediately on
the first kill-switch invoke smoke.

The audit chain is currently empty in production (``CHAIN OK: 0 rows
verified`` per the Day-12 carryover smoke + every subsequent verify),
so no historical data is affected. The fix simply unblocks the writer
going forward.

Operational migration per dev-guide §7.1 hybrid scheme. Idempotent: a
re-run on a schema that already has the grant is a no-op (Postgres
GRANT is idempotent).

A02 BINDS — ``alembic/**`` requires ``risk-review-approved`` PR label.

Revision ID: 20260518_audit_seq_grant
Revises: 20260513_auth_tables
Create Date: 2026-05-18 20:30:00.000000

Note: revision id kept short (24 chars) because Alembic's default
``alembic_version.version_num`` column is ``VARCHAR(32)``. The fuller
``20260518_audit_log_sequence_grant`` (33 chars) overflows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260518_audit_seq_grant"
down_revision: str | None = "20260513_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Grant USAGE on the audit_log BIGSERIAL sequence to app_service.

    Two grants:

    1. Immediate fix: USAGE + SELECT on the existing
       ``audit_log_sequence_no_seq``. INSERT into ``audit_log`` issues
       ``nextval('audit_log_sequence_no_seq')`` under the hood via the
       BIGSERIAL DEFAULT, and ``nextval`` requires USAGE.
    2. Future-proof: ``ALTER DEFAULT PRIVILEGES ... ON SEQUENCES`` so
       any future BIGSERIAL/SERIAL/CREATE SEQUENCE picks up the same
       grant automatically. Mirrors the ``ON TABLES`` default-privilege
       grant at ``alembic/versions/0006_roles.py:88-90``.

    Note: as of 2026-05-18 the only sequence in the schema is
    ``audit_log_sequence_no_seq`` (every other PK uses
    ``uuid_generate_v7()``). The future-proof grant is documented
    insurance, not a fix for an existing gap beyond the one named
    sequence.

    Idempotent: Postgres GRANT is idempotent, so a re-run on a
    schema that already has the grant is a no-op.
    """
    op.execute("GRANT USAGE, SELECT ON SEQUENCE audit_log_sequence_no_seq TO app_service")
    # Future-proofing for any sequence created by subsequent migrations
    # under the same owner (postgres SUPERUSER). The ``GRANTED BY`` clause
    # is omitted; Postgres uses the current role as the grantor.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO app_service
        """
    )


def downgrade() -> None:
    """Revoke the sequence grants.

    Strictly the reverse of upgrade. Note: revoking USAGE will re-break
    the audit writer (this is the bug the upgrade fixes), so a downgrade
    is only safe before any audit-log writes have happened on the
    target environment. The downgrade exists for completeness; in
    practice no one will run it because the upgrade is unblocking a
    locked behaviour.
    """
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        REVOKE USAGE, SELECT ON SEQUENCES FROM app_service
        """
    )
    op.execute("REVOKE USAGE, SELECT ON SEQUENCE audit_log_sequence_no_seq FROM app_service")
