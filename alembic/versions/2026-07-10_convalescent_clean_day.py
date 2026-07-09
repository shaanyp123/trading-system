"""Add ``risk_state.convalescent_last_counted_day_utc`` (clean-day tick marker).

Crypto-pivot §3.4 / decisions-log 2026-07-09 (C1 night one, CONVALESCENT
amendment): the 00:15 UTC recon cycle is the production caller of the
CONVALESCENT clean-day tick. This column is the tick's once-per-UTC-day
idempotency marker: it records the most recent UTC calendar day already
counted for the CURRENT CONVALESCENT stint, so a scheduler re-fire or a
crash-restart re-running the cycle on the same day cannot double-count.

Why a durable column and not process memory: the recon scheduler restarts
with the api container; an in-memory "already ticked today" latch would
reset on every deploy and double-count. Why DATE and not TIMESTAMPTZ: the
unit being counted IS the UTC calendar day (delta spec §3.4 "clean UTC
calendar days"); storing a timestamp would invite accidental sub-day
comparisons.

Nullable by design: NULL = no day counted yet for this stint. Rows written
by state transitions (services/risk/dispatch.py) leave it NULL; only the
tick's counter UPDATE sets it.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260710_conv_clean_day
Revises: 20260709_strategy_worker
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260710_conv_clean_day"
down_revision: str | None = "20260709_strategy_worker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable DATE marker column."""
    op.execute("ALTER TABLE risk_state ADD COLUMN convalescent_last_counted_day_utc DATE")


def downgrade() -> None:
    """Drop the marker column ([A16] rollback contract)."""
    op.execute("ALTER TABLE risk_state DROP COLUMN IF EXISTS convalescent_last_counted_day_utc")
