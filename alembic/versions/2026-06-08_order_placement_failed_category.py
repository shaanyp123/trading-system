"""Add `order_placement_failed` value to alert_category enum.

Silent-failure follow-up (2026-06-08). The ``order_placement_worker``
drains ``status='approved'`` signals into IBKR orders. When a placement
raises ``IbkrPlacementError`` — broker unavailable OR a contract
rejection (e.g. the 2026-06-04 /MYM order: ``resolve_contract`` sent
``exchange="CME"`` for a CBOT contract → IBKR Error 200, fixed in #327) —
the worker logged ``order_placement_broker_unavailable`` and retried, but
fired NO alert. A failed order on an operator-approved signal was
therefore silent (the ``async_task_monitor`` watches worker *death*, not
per-order broker errors). This enum value backs a new P1 ``#alerts``
notification emitted from the worker's ``IbkrPlacementError`` catch via
an optional alert hook wired in ``services/api/main.py``.

Without this migration the INSERT would raise ``invalid input value for
enum alert_category: 'order_placement_failed'`` and the hook would
swallow it (it fails open), leaving the operator with no notification —
the very gap this closes.

Mirrors ``alembic/versions/2026-05-26_worker_failure_alert_category.py``
verbatim. Idempotent via ``ADD VALUE IF NOT EXISTS`` (PG 12+). Downgrade
is intentionally a no-op (PostgreSQL < 15 cannot remove enum values
cleanly).

The mirror Python enum value lives at
``services/webhook_pusher/payloads.py::AlertCategory.ORDER_PLACEMENT_FAILED``;
they must stay in sync (test
``tests/unit/test_webhook_pusher.py::test_alert_category_count_matches_spec``
enforces the count at lint time).

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260608_order_placement_failed
Revises: 20260602_efficiency_ratio_gate
Create Date: 2026-06-08 19:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260608_order_placement_failed"
down_revision: str | None = "20260602_efficiency_ratio_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``order_placement_failed`` to ``alert_category`` enum.

    ``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is the canonical idempotent
    enum-extension pattern for PostgreSQL 12+. Appends to the end of the
    enum's sort order (no ``BEFORE``/``AFTER`` qualifier).
    """
    op.execute("ALTER TYPE alert_category ADD VALUE IF NOT EXISTS 'order_placement_failed'")


def downgrade() -> None:
    """No-op — PostgreSQL < 15 cannot DROP an enum value cleanly.

    Matches the downgrade contract of the prior alert_category extension
    migrations (heartbeat_stale, worker_failure, position_unprotected,
    reconciliation_data_source_degraded).
    """
