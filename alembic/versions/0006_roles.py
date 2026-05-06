"""Postgres role hierarchy: app_service, app_service_readonly, app_owner, dba_breakglass.

Implements backend-spec §8.2.

Roles are created NOLOGIN here. Passwords are set out-of-band at deploy time
by a bootstrap step that decrypts `secrets/<env>.enc.yaml` and runs:

    ALTER ROLE app_service WITH LOGIN PASSWORD '<from-sops>';
    ALTER ROLE app_owner   WITH LOGIN PASSWORD '<from-sops>';
    ...

This migration deliberately keeps zero plaintext credentials in the repo.
`dba_breakglass` is created with SUPERUSER NOLOGIN; the operator activates
it manually with the paper credential per spec §8.2.1.

Idempotent guards (`DO ... IF NOT EXISTS`) let this migration be re-run on
an already-bootstrapped database — useful when restoring from a backup that
predates the migration.

Closes the per-role grants from 0005 (REVOKE TRUNCATE on audit_log from
app_service / app_owner; GRANT to dba_breakglass).

Revision ID: 0006_roles
Revises: 0005_immutability
Create Date: 2026-05-05 12:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_roles"
down_revision: str | None = "0005_immutability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ROLES_NOLOGIN: tuple[str, ...] = (
    "app_service",
    "app_service_readonly",
    "app_owner",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Create roles (NOLOGIN; passwords set out-of-band from sops)
    # ------------------------------------------------------------------
    for role in _ROLES_NOLOGIN:
        # `role` is a constant identifier from `_ROLES_NOLOGIN` — never user
        # input. SQL injection is impossible here; the f-string is the
        # idiomatic way to emit a CREATE ROLE statement against a known name.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN;
                END IF;
            END
            $$
            """  # noqa: S608
        )

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dba_breakglass') THEN
                CREATE ROLE dba_breakglass SUPERUSER NOLOGIN;
            END IF;
        END
        $$
        """
    )

    # ------------------------------------------------------------------
    # Grants per spec §8.2
    # app_service: INSERT/SELECT on audit_log (NEVER UPDATE/DELETE/TRUNCATE);
    #              full DML elsewhere.
    # ------------------------------------------------------------------
    op.execute("GRANT USAGE ON SCHEMA public TO app_service")
    op.execute("GRANT INSERT, SELECT ON audit_log TO app_service")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM app_service")
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON ALL TABLES IN SCHEMA public TO app_service
        """
    )
    # Re-revoke the audit_log mutating grants that the wildcard above re-added.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM app_service")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_service
        """
    )

    # ------------------------------------------------------------------
    # app_service_readonly: SELECT on everything; agent's read-only sub-role
    # ------------------------------------------------------------------
    op.execute("GRANT USAGE ON SCHEMA public TO app_service_readonly")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_service_readonly")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT ON TABLES TO app_service_readonly
        """
    )

    # ------------------------------------------------------------------
    # app_owner: full schema ownership; runs Alembic; cannot bypass triggers
    # ------------------------------------------------------------------
    op.execute("GRANT ALL ON SCHEMA public TO app_owner")
    op.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO app_owner")
    op.execute("REVOKE TRUNCATE ON audit_log FROM app_owner")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT ALL ON TABLES TO app_owner
        """
    )

    # ------------------------------------------------------------------
    # dba_breakglass: only role retaining TRUNCATE on audit_log (spec §2.10.2)
    # ------------------------------------------------------------------
    op.execute("GRANT TRUNCATE ON audit_log TO dba_breakglass")


def downgrade() -> None:
    # Order matters: revoke before drop role (Postgres refuses DROP ROLE while
    # privileges remain).
    op.execute("REVOKE ALL PRIVILEGES ON audit_log FROM dba_breakglass")
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM app_owner
        """
    )
    op.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM app_owner")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        REVOKE ALL ON TABLES FROM app_owner
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM app_service_readonly
        """
    )
    op.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM app_service_readonly")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        REVOKE ALL ON TABLES FROM app_service_readonly
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM app_service
        """
    )
    op.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM app_service")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        REVOKE ALL ON TABLES FROM app_service
        """
    )

    op.execute("DROP ROLE IF EXISTS dba_breakglass")
    for role in _ROLES_NOLOGIN:
        op.execute(f"DROP ROLE IF EXISTS {role}")
