"""Add `reconciliation_data_source_degraded` value to alert_category enum.

Option C recon-fix follow-up (2026-05-29). The EOD reconciliation cycle
(``services/reconciliation/eod_cycle.py::run_eod_cycle``) gained a
``position_source="reqpositions"`` branch in PR-B
(``feat/option-c-recon-reqpositions-prb``) that sources the broker
position list from IBKR's real-time TWS API (``reqPositions`` via
clientId=4) to kill same-day-fill settlement-lag false breaks. When that
per-cycle fetch fails terminally the cycle falls back to the FlexQuery
position list and — as of this follow-up — INSERTs an ``alerts`` row with
``category = 'reconciliation_data_source_degraded'`` (severity ``P1`` →
Discord #alerts) so the silent fallback pages the operator instead of
only surfacing on a structlog scan.

Without this migration the INSERT would raise ``invalid input value for
enum alert_category: 'reconciliation_data_source_degraded'`` and the
degraded-source alert helper would log a dispatch failure with no
operator notification — defeating the entire point of the follow-up.

Mirrors ``alembic/versions/2026-05-27_position_unprotected.py`` verbatim.
Idempotent via ``ADD VALUE IF NOT EXISTS`` (PG 12+). Downgrade is
intentionally a no-op (PostgreSQL < 15 cannot remove enum values
cleanly).

NOTE: the revision id is the abbreviated ``recon_src_degraded`` (not the
full ``reconciliation_data_source_degraded``) because ``alembic_version
.version_num`` is ``VARCHAR(32)`` — the full slug overflows it. The
*enum value* the migration adds is the full string; only the migration's
own identifier is shortened.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260529_recon_src_degraded
Revises: 20260528_signal_proximity
Create Date: 2026-05-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260529_recon_src_degraded"
down_revision: str | None = "20260528_signal_proximity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``reconciliation_data_source_degraded`` to ``alert_category`` enum.

    ``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is the canonical idempotent
    enum-extension pattern for PostgreSQL 12+. Sets the new value to the
    end of the enum's sort order (no ``BEFORE``/``AFTER`` qualifier).

    The mirror Python enum value lives at
    ``services/webhook_pusher/payloads.py::AlertCategory.RECONCILIATION_DATA_SOURCE_DEGRADED``;
    they must stay in sync (test
    ``tests/unit/test_webhook_pusher.py::test_alert_category_count_matches_spec``
    enforces the count + value list at lint time).
    """
    op.execute(
        "ALTER TYPE alert_category ADD VALUE IF NOT EXISTS 'reconciliation_data_source_degraded'"
    )


def downgrade() -> None:
    """No-op: PostgreSQL < 15 cannot remove enum values cleanly.

    Intentionally a no-op so an alembic downgrade -1 against this
    migration succeeds without touching the type. See the head note on
    ``2026-05-17_heartbeat_stale_category.py`` for the manual-removal
    runbook if the operator truly needs to drop the value.
    """
    # No-op by design (see docstring).
