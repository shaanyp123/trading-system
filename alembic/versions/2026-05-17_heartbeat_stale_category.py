"""Add `heartbeat_stale` value to alert_category enum (PR #154 follow-up).

The heartbeat-staleness probe (``services/api/heartbeat_probe.py``)
fires alerts when a tracked cron-like service hasn't recorded a
heartbeat within its locked threshold. Those alerts INSERT into the
``alerts`` table with ``category = 'heartbeat_stale'``; the column is
typed as the ``alert_category`` PostgreSQL ENUM, which is locked at
its 29 prior values per ``alembic/versions/0004_ops_tables.py:259``.

Without this migration the INSERT would raise
``invalid input value for enum alert_category: 'heartbeat_stale'`` and
the staleness probe would silently swallow the failure (its dispatch
hook is wrapped in try/except) — leaving the operator with no Discord
notification when LEAN goes stale.

Operational migration per dev-guide §7.1 hybrid scheme. Idempotent:
``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` (PG 12+) is a no-op when
the value already exists.

**Downgrade is intentionally a no-op.** PostgreSQL pre-15 does not
support removing values from an enum type — you'd need to recreate
the type + re-cast every column using it, which is a much heavier
operation than this migration is ever rolling back. If the operator
needs to remove the value, do it via a hand-crafted migration that
asserts no rows in alerts use the value first.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
`risk-review-approved` required.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260517_heartbeat_stale"
down_revision: str | None = "20260518_audit_seq_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``heartbeat_stale`` to ``alert_category`` enum.

    ``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is the canonical
    idempotent enum-extension pattern for PostgreSQL 12+. Sets the new
    value to the end of the enum's sort order (no ``BEFORE``/``AFTER``
    qualifier).

    The mirror Python enum value lives at
    ``services/webhook_pusher/payloads.py::AlertCategory.HEARTBEAT_STALE``;
    they must stay in sync (test
    ``tests/unit/test_webhook_pusher.py::test_alert_category_count_matches_spec``
    enforces the count + value list at lint time).
    """
    op.execute("ALTER TYPE alert_category ADD VALUE IF NOT EXISTS 'heartbeat_stale'")


def downgrade() -> None:
    """No-op: PostgreSQL < 15 cannot remove enum values cleanly.

    Intentionally a no-op so an alembic downgrade -1 against this
    migration succeeds without touching the type. If the operator
    needs to truly remove the value (e.g., for a clean test fixture
    rebuild), drop + recreate the alerts table OR write a custom
    migration that:

      1. ``ALTER TABLE alerts ALTER COLUMN category TYPE text;``
      2. ``DROP TYPE alert_category;``
      3. ``CREATE TYPE alert_category AS ENUM (...)`` with the prior list
      4. ``ALTER TABLE alerts ALTER COLUMN category TYPE alert_category USING category::alert_category;``
    """
    # No-op by design (see docstring).
