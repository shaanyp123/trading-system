"""Auth tables: webauthn_credentials, totp_secrets, backup_codes, sessions.

Implements backend-spec §8.5.1 (WebAuthn primary), §8.5.2 (TOTP backup + 8
single-use backup codes), and §8.5.3 (session cookies + server-side row).

Day 21 / Week 6 Tue. The Day 3 foundational migrations 0001-0006 are sealed;
this is the second operational migration after ``20260509_qc_adapter_cursor_seed``.

Raw SQL is used throughout to keep this migration 1:1 with backend-spec §8.5.
The spec is the source of truth; comparing SQL blocks side-by-side is the
review path. Mirrors the same pattern as ``0002_core_tables.py``.

FK conventions (matched to spec §8.5):
    - webauthn_credentials.account_id → accounts(id)  (spec explicit)
    - totp_secrets.account_id         → accounts(id)  (spec explicit; PK)
    - backup_codes.account_id         → accounts(id)  (spec is silent; FK
      added for referential consistency — the column is documented as
      pointing at accounts; without an FK an orphan row could survive an
      account purge.)
    - sessions.account_id             → accounts(id)  (spec is silent; same
      reasoning. Soft-delete via ``accounts.active_to`` means cascading
      delete is not the right behaviour — we keep the default NO ACTION
      so an attempted hard delete of an account with active sessions
      fails loudly rather than silently revoking the audit trail.)

Grant inheritance: ``0006_roles.py:97-100`` set ``ALTER DEFAULT PRIVILEGES
... GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_service`` (plus
matching grants for ``app_service_readonly`` SELECT and ``app_owner`` ALL).
New tables created here inherit those grants automatically — no manual
per-table GRANT needed. Verified against 0006 lines 95-125.

A06 enforced: every timestamp column is ``TIMESTAMPTZ``.
A05 N/A: no Decimal columns.
A02 BINDS: ``alembic/**`` is on dev-guide §2.2 forbidden whitelist; this PR
requires ``risk-review-approved`` label.

Revision ID: 20260513_auth_tables
Revises: 20260509_qc_adapter_cursor_seed
Create Date: 2026-05-13 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260513_auth_tables"
down_revision: str | None = "20260509_qc_adapter_cursor_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # webauthn_credentials (backend-spec §8.5.1)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE webauthn_credentials (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            credential_id BYTEA NOT NULL UNIQUE,
            public_key BYTEA NOT NULL,
            sign_count INTEGER NOT NULL DEFAULT 0,
            authenticator_attachment TEXT,
            registered_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at_utc TIMESTAMPTZ,
            nickname TEXT,
            active BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_webauthn_credentials_account "
        "ON webauthn_credentials(account_id) WHERE active = TRUE"
    )

    # ------------------------------------------------------------------
    # totp_secrets (backend-spec §8.5.2)
    # encrypted_secret is column-encrypted at the app layer (key separate
    # from sops); the DB stores ciphertext bytes only.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE totp_secrets (
            account_id UUID PRIMARY KEY REFERENCES accounts(id),
            encrypted_secret BYTEA NOT NULL,
            enrolled_at_utc TIMESTAMPTZ NOT NULL,
            last_used_at_utc TIMESTAMPTZ
        )
        """
    )

    # ------------------------------------------------------------------
    # backup_codes (backend-spec §8.5.2)
    # 8 codes per generation; Argon2id-hashed (argon2-cffi); raw codes
    # never persisted. `generation_id` ties a batch of 8 codes together
    # so regeneration can invalidate the prior batch.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE backup_codes (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            code_hash TEXT NOT NULL,
            used_at_utc TIMESTAMPTZ,
            generation_id UUID NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_backup_codes_account_unused "
        "ON backup_codes(account_id) WHERE used_at_utc IS NULL"
    )
    op.execute("CREATE INDEX idx_backup_codes_generation ON backup_codes(generation_id)")

    # ------------------------------------------------------------------
    # sessions (backend-spec §8.5.3)
    # `id` is the raw session_id bytes (random 32 bytes). The session
    # cookie stores its base64url encoding; the DB lookup decodes back to
    # BYTEA. Idle timeout enforced by `last_activity_utc` comparison;
    # absolute timeout enforced by `expires_at_utc`. `last_uv_at_utc` is
    # the WebAuthn UV timestamp consumed by the 5-min re-auth window
    # (dev-guide §1.5). `totp_bootstrap_only = TRUE` maps to application-
    # layer `auth_strength = "weak"` per frontend-spec §5.4.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE sessions (
            id BYTEA PRIMARY KEY,
            account_id UUID NOT NULL REFERENCES accounts(id),
            created_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_activity_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_uv_at_utc TIMESTAMPTZ,
            user_agent TEXT,
            ip_address INET,
            totp_bootstrap_only BOOLEAN NOT NULL DEFAULT FALSE,
            evicted_at_utc TIMESTAMPTZ,
            evicted_reason TEXT,
            expires_at_utc TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX sessions_account ON sessions(account_id)")
    op.execute("CREATE INDEX sessions_expiry ON sessions(expires_at_utc)")


def downgrade() -> None:
    # Order: child tables first (none here — no FKs reference these
    # tables yet), then drop in reverse-creation order. CASCADE not used
    # so unexpected dependencies surface loudly rather than silently
    # dropping referencing objects.
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("DROP TABLE IF EXISTS backup_codes")
    op.execute("DROP TABLE IF EXISTS totp_secrets")
    op.execute("DROP TABLE IF EXISTS webauthn_credentials")
