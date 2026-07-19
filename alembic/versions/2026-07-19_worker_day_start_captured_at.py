"""Add ``strategy_worker_status.day_start_captured_at_utc`` (sweep-window anchor).

Cash-sweep consumer adjustments (C1→C2 follow-up PR 2; the
``deploy/cash_manager/README.md`` HARD GATE): the daily-loss check nets
completed ``cash_sweeps`` out of the ``equity / day_start_equity`` ratio,
windowed by *when the baseline was captured*. ``day_start_date`` alone
cannot anchor that window — the baseline is captured at the first
successful equity read of the UTC day, which is ~00:00:30 in normal
operation but can be mid-day after downtime; a sweep completed BEFORE a
mid-day baseline capture is already reflected in the baseline and must
NOT be netted again (double-netting a ``to_yield`` sweep would inflate
the adjusted P&L — a loosening). Persisting the capture instant makes
the window exact across worker restarts.

Nullable by design: rows written before this migration have no capture
instant. The worker treats NULL as "window anchor unknown" and falls
back to the UNADJUSTED (pre-PR) daily-loss computation for that
baseline (operator-directed fail-safe direction). The NULL heals at
the **next UTC-day rollover** — the anchor is only minted alongside a
fresh baseline (the status upsert persists whatever is in memory, which
stays None until the rollover), so the deploy day runs unadjusted end
to end; the once-per-(consumer, day) rate-limited WARNING names it
(risk-review 2026-07-19 finding 1).

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required. Full ``downgrade()`` per [A16].

Revision ID: 20260719_day_start_captured_at
Revises: 20260713_recon_resolution_auto
Create Date: 2026-07-19 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260719_day_start_captured_at"
down_revision: str | None = "20260713_recon_resolution_auto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable capture-instant column."""
    op.execute(
        "ALTER TABLE strategy_worker_status ADD COLUMN day_start_captured_at_utc TIMESTAMPTZ"
    )


def downgrade() -> None:
    """Drop the capture-instant column (worker falls back to unadjusted)."""
    op.execute("ALTER TABLE strategy_worker_status DROP COLUMN IF EXISTS day_start_captured_at_utc")
