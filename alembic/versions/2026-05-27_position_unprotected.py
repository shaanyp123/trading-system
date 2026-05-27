"""Add `position_unprotected` value to alert_category enum (exit-pipeline PR-C).

The exit-pipeline `order_placement_worker` exit branch (PR-C, landed
2026-05-27) INSERTs an ``alerts`` row with
``category = 'position_unprotected'`` when the bracket-stop CANCEL
succeeds but the subsequent close-order PLACE fails — leaving the
position NAKED (no protective stop). This is the worst failure mode in
the exit pipeline (R3 in ``Docs/exit-pipeline-design.md`` §11).

Without this migration the INSERT would raise ``invalid input value
for enum alert_category: 'position_unprotected'`` and the worker's
failure handler would crash before the Discord #critical + Resend
email push, leaving the operator with no notification AND a naked
position with no audit/alert breadcrumb.

Mirrors ``alembic/versions/2026-05-26_worker_failure_alert_category.py``
verbatim. Idempotent via ``ADD VALUE IF NOT EXISTS`` (PG 12+).
Downgrade is intentionally a no-op (PostgreSQL < 15 cannot remove
enum values cleanly).

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260527_position_unprotected
Revises: 20260526_worker_failure
Create Date: 2026-05-27 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260527_position_unprotected"
down_revision: str | None = "20260526_worker_failure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``position_unprotected`` to ``alert_category`` enum.

    ``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is the canonical
    idempotent enum-extension pattern for PostgreSQL 12+. Sets the new
    value to the end of the enum's sort order (no ``BEFORE``/``AFTER``
    qualifier).

    The mirror Python enum value lives at
    ``services/webhook_pusher/payloads.py::AlertCategory.POSITION_UNPROTECTED``;
    they must stay in sync (test
    ``tests/unit/test_webhook_pusher.py::test_alert_category_count_matches_spec``
    enforces the count + value list at lint time).
    """
    op.execute("ALTER TYPE alert_category ADD VALUE IF NOT EXISTS 'position_unprotected'")


def downgrade() -> None:
    """No-op: PostgreSQL < 15 cannot remove enum values cleanly.

    Intentionally a no-op so an alembic downgrade -1 against this
    migration succeeds without touching the type. See the head note on
    ``2026-05-17_heartbeat_stale_category.py`` for the manual-removal
    runbook if the operator truly needs to drop the value.
    """
    # No-op by design (see docstring).
