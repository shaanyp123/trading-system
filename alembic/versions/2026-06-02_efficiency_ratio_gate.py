"""Add ``efficiency_ratio_*`` columns to ``signal_proximity`` for the ER gate swap.

V1 entry gate #3 swapped from the Hurst R/S persistence estimator to the
Kaufman Efficiency Ratio (ER), LIVE at ``EFFICIENCY_RATIO_THRESHOLD=0.20``
from cycle one (decisions-log.md 2026-06-02 + the gate-swap PR description).
This migration evolves the ``signal_proximity`` table (the /signals
"Watching" view's durable store) to carry the new gate's columns.

**What changes**

  * ADD ``efficiency_ratio_value``     NUMERIC NULL — the raw ER in [0, 1]
    (NULL when warming up). Populated so the live ER distribution is
    mineable directly from this table — the interim calibration evidence
    confirming/adjusting the 0.20 launch threshold (no backtester yet).
  * ADD ``efficiency_ratio_threshold`` NUMERIC NULL — the active threshold
    at evaluation time (self-describing rows; agent may tighten it up).
  * ADD ``efficiency_ratio_state``     TEXT NOT NULL CHECK IN
    ('pass'|'close'|'fail') — the per-gate state (mirrors ``hurst_state``).
    Existing rows are backfilled to the warming-up sentinel ``'fail'`` via a
    transient column DEFAULT that is then DROPped, so future inserts MUST
    specify the value (matching the no-default shape of the other state
    columns created in ``20260528_signal_proximity``).
  * ADD ``efficiency_ratio_headroom``  NUMERIC NULL — value minus threshold,
    in ER units (NULL when warming up); mirrors ``hurst_headroom``.
  * ALTER ``hurst_state`` DROP NOT NULL — new rows no longer compute Hurst,
    so they leave the ``hurst_*`` columns NULL.
  * WIDEN the ``closest_gate`` CHECK to add ``'efficiency'`` while KEEPING
    ``'hurst'`` (historic rows emitted before the swap retain it).

**What is RETAINED (D2)** — the four ``hurst_*`` columns (``hurst_value``,
``hurst_threshold``, ``hurst_state``, ``hurst_headroom``) are NOT dropped.
Keeping them (a) preserves historic proximity rows for forensic review and
(b) is the rollback substrate: redeploying the prior build resumes writing
``hurst_*`` on the next daily cycle without a schema change. See the PR
ROLLBACK section.

**No new SSE / audit event types.** Proximity rows are observational (Q2 of
signal-proximity-design.md, locked) — they ARE their own audit trail; no
audit-first row is gated before the write. This migration touches only the
``signal_proximity`` storage shape.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260602_efficiency_ratio_gate
Revises: 20260531_exit_proximity
Create Date: 2026-06-02 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260602_efficiency_ratio_gate"
down_revision: str | None = "20260531_exit_proximity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The auto-generated name Postgres assigns the inline ``closest_gate`` column
#: CHECK created in ``20260528_signal_proximity`` (``<table>_<column>_check``).
#: Dropping by this exact name lets us widen the allowed set in place.
_CLOSEST_GATE_CHECK = "signal_proximity_closest_gate_check"


def upgrade() -> None:
    """Evolve ``signal_proximity`` for the Hurst → Efficiency-Ratio gate swap.

    Idempotent-friendly ordering: add the new columns first (the NOT NULL
    state column backfills existing rows via a transient DEFAULT, then drops
    it), relax the now-optional ``hurst_state``, then widen the
    ``closest_gate`` CHECK.
    """
    # --- New ER columns -------------------------------------------------
    op.execute("ALTER TABLE signal_proximity ADD COLUMN efficiency_ratio_value NUMERIC")
    op.execute("ALTER TABLE signal_proximity ADD COLUMN efficiency_ratio_threshold NUMERIC")
    op.execute("ALTER TABLE signal_proximity ADD COLUMN efficiency_ratio_headroom NUMERIC")

    # NOT NULL state column on a populated table: add with a transient
    # DEFAULT 'fail' (the warming-up sentinel — historic rows had no ER
    # computed, so 'fail' is the honest backfill), then DROP the default so
    # future inserts must supply the value, matching the no-default shape of
    # the per-gate state columns from 20260528_signal_proximity.
    op.execute(
        "ALTER TABLE signal_proximity "
        "ADD COLUMN efficiency_ratio_state TEXT NOT NULL DEFAULT 'fail' "
        "CHECK (efficiency_ratio_state IN ('pass', 'close', 'fail'))"
    )
    op.execute("ALTER TABLE signal_proximity ALTER COLUMN efficiency_ratio_state DROP DEFAULT")

    # --- Relax the now-optional Hurst state -----------------------------
    # New rows leave hurst_* NULL (the gate no longer computes Hurst). The
    # columns are RETAINED for historic rows + rollback.
    op.execute("ALTER TABLE signal_proximity ALTER COLUMN hurst_state DROP NOT NULL")

    # --- Widen the closest_gate CHECK -----------------------------------
    # Add 'efficiency'; KEEP 'hurst' so historic rows stay valid.
    op.execute(f"ALTER TABLE signal_proximity DROP CONSTRAINT IF EXISTS {_CLOSEST_GATE_CHECK}")
    op.execute(
        f"ALTER TABLE signal_proximity ADD CONSTRAINT {_CLOSEST_GATE_CHECK} "
        "CHECK (closest_gate IN ('donchian', 'trend', 'hurst', 'efficiency', 'history'))"
    )


def downgrade() -> None:
    """Reverse the upgrade. Lossy by nature — reverting a column addition
    discards the ER data and cannot reconstruct a pre-swap ``hurst_state``
    for rows the new code wrote.

    Best-effort, non-crashing restore:
      1. Re-map any ``closest_gate='efficiency'`` rows to ``'history'`` (the
         neutral warming-up sentinel — 'efficiency' did not exist pre-swap),
         then restore the original CHECK without 'efficiency'.
      2. Backfill NULL ``hurst_state`` (rows the new code wrote) to the
         ``'fail'`` sentinel, then re-assert NOT NULL.
      3. Drop the four ``efficiency_ratio_*`` columns.
    """
    # 1. closest_gate CHECK back to the original allowed set.
    op.execute(
        "UPDATE signal_proximity SET closest_gate = 'history' WHERE closest_gate = 'efficiency'"
    )
    op.execute(f"ALTER TABLE signal_proximity DROP CONSTRAINT IF EXISTS {_CLOSEST_GATE_CHECK}")
    op.execute(
        f"ALTER TABLE signal_proximity ADD CONSTRAINT {_CLOSEST_GATE_CHECK} "
        "CHECK (closest_gate IN ('donchian', 'trend', 'hurst', 'history'))"
    )

    # 2. Re-assert hurst_state NOT NULL (backfill the rows that left it NULL).
    op.execute("UPDATE signal_proximity SET hurst_state = 'fail' WHERE hurst_state IS NULL")
    op.execute("ALTER TABLE signal_proximity ALTER COLUMN hurst_state SET NOT NULL")

    # 3. Drop the ER columns (their CHECK drops implicitly with the column).
    op.execute("ALTER TABLE signal_proximity DROP COLUMN IF EXISTS efficiency_ratio_headroom")
    op.execute("ALTER TABLE signal_proximity DROP COLUMN IF EXISTS efficiency_ratio_state")
    op.execute("ALTER TABLE signal_proximity DROP COLUMN IF EXISTS efficiency_ratio_threshold")
    op.execute("ALTER TABLE signal_proximity DROP COLUMN IF EXISTS efficiency_ratio_value")
