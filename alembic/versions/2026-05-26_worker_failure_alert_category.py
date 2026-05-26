"""Add `worker_failure` value to alert_category enum (recovery-agent PR).

The recovery agent (`scripts/operator_tools/recovery_agent.py`, landed
2026-05-26) polls the ``alerts`` table for rows with
``category = 'worker_failure'`` — INSERTed by the new task-death hook
in ``services/api/async_task_monitor.py`` when a tracked lifespan task
(today: ``order_placement_worker.run_forever``) transitions to
``.done()`` unexpectedly. Without this migration the INSERT would raise
``invalid input value for enum alert_category: 'worker_failure'`` and
the monitor's death hook would silently swallow the failure (its
``_schedule_alert_dispatch`` helper is wrapped in try/except so a hook
crash doesn't kill the probe loop) — leaving the operator with no
Discord notification AND no recovery-agent action when the worker
dies.

Drill 5 (2026-05-18) closed via manual ``/tmp/drill5_recovery.py``
invocation; drill 7 (2026-05-18) the same way; this enum value + the
new audit event types (``async_task_died``, ``recovery_action_taken``,
landed in the same PR) together let the recovery agent fully
automate that path.

Mirrors ``alembic/versions/2026-05-17_heartbeat_stale_category.py``
verbatim. Idempotent via ``ADD VALUE IF NOT EXISTS`` (PG 12+).
Downgrade is intentionally a no-op (PostgreSQL < 15 cannot remove
enum values cleanly).

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260526_worker_failure
Revises: 20260517_heartbeat_stale
Create Date: 2026-05-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260526_worker_failure"
down_revision: str | None = "20260517_heartbeat_stale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``worker_failure`` to ``alert_category`` enum.

    ``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is the canonical
    idempotent enum-extension pattern for PostgreSQL 12+. Sets the new
    value to the end of the enum's sort order (no ``BEFORE``/``AFTER``
    qualifier).

    The mirror Python enum value lives at
    ``services/webhook_pusher/payloads.py::AlertCategory.WORKER_FAILURE``;
    they must stay in sync (test
    ``tests/unit/test_webhook_pusher.py::test_alert_category_count_matches_spec``
    enforces the count + value list at lint time).
    """
    op.execute("ALTER TYPE alert_category ADD VALUE IF NOT EXISTS 'worker_failure'")


def downgrade() -> None:
    """No-op: PostgreSQL < 15 cannot remove enum values cleanly.

    Intentionally a no-op so an alembic downgrade -1 against this
    migration succeeds without touching the type. See the head note on
    ``2026-05-17_heartbeat_stale_category.py`` for the manual-removal
    runbook if the operator truly needs to drop the value.
    """
    # No-op by design (see docstring).
