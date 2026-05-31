"""Create ``exit_proximity`` table for the Today "Exits" view (PR-B).

PR-B of ``Docs/exit-proximity-design.md`` (SIGNED OFF 2026-05-30). Stores the
per-OPEN-POSITION exit-proximity records LEAN emits on every daily
``lean_cycle_heartbeat`` (21:30 UTC) — the mirror image of ``signal_proximity``
(entry-side "Watching") for the exit side: for each held position, how close is
it to being CLOSED by each of the four V1 exit triggers (stop / reversal /
trend_flip / decommission).

**Why a dedicated table** — ED5 of the design doc: per-position shape (held_days
/ stop distance / per-trigger exit states) is distinct from the per-market
entry-proximity shape, so reusing ``signal_proximity`` or stuffing JSONB on the
heartbeat would couple two orthogonal surfaces. A dedicated table is queryable +
indexable; cost is this one migration.

**Why no audit_log row gated before write** — Q6 of the design doc (accepted at
sign-off; mirrors ``signal_proximity`` Q2): exit-proximity rows are observational
(LEAN reporting what it saw on a daily evaluation), NOT a state change. The table
IS the durable trail. The audit-first ordering rule
(``feedback_audit_first_ordering`` memory) is for state-change events; this
carve-out is explicit and locked.

**Q1 = (B) — api-side stop join.** LEAN emits the indicator dimensions only and
sends ``stop_price=None`` / a NULL-state stop. The PR-B heartbeat handler joins
the latest WORKING ``stop_market`` order for the market (``orders`` table) to
populate ``stop_price`` + re-classifies ``stop_state``/``stop_headroom_pct`` via
``exit_proximity.classify_stop``, then RE-DERIVES ``overall_state``/
``closest_exit`` via ``exit_proximity.combine_overall_exit`` so the persisted row
is internally consistent (a NEAR/TRIGGERED stop can flip the overall state away
from the stop-blind value LEAN computed).

**Retention** — keep all rows. Volume = ~2 open positions x 1 cycle/day = ~2
rows/day. Storage cost is negligible.

**CHECK-constraint values** — pinned to the Python enums in
``strategies/v1_trend_following/exit_proximity.py`` AND the wire-shape Literals in
``services/api/schemas/lean.py`` so any future drift (e.g. a new exit state) is
caught by a migration mismatch rather than silently accepted:

  * per-trigger + overall state columns → ``ExitState``
      ('holding' | 'near' | 'triggered')
  * ``closest_exit``  → 'stop' | 'reversal' | 'trend_flip' | 'decommission'
  * ``gate_status``   → 'active' | 'warming_up' | 'min_holding_blocked'
                        | 'decommissioned'
  * ``direction``     → 'long' | 'short'

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260531_exit_proximity
Revises: 20260529_recon_src_degraded
Create Date: 2026-05-31 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260531_exit_proximity"
down_revision: str | None = "20260529_recon_src_degraded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``exit_proximity`` table + its two read indexes.

    No FK to ``accounts`` — proximity rows are scoped to the algorithm /
    universe (which open positions LEAN evaluated this cycle), not a particular
    account, identical to ``signal_proximity``. A future multi-account
    expansion can add the FK without invalidating existing rows.

    ``id`` is ``BIGSERIAL`` (not ``UUID``) because the table is append-only and
    never referenced by FK; ``BIGSERIAL`` is sufficient and saves the
    ``uuid_generate_v7()`` call per row.

    The numeric / held_days columns are NULLable to support the warming-up case
    (``gate_status='warming_up'`` is the discriminator) and the "no working stop
    on record" case (``stop_price``/``stop_headroom_pct`` NULL + ``stop_state``
    carries the conservative HOLDING NULL-state from ``classify_stop`` — this is
    also a latent POSITION_UNPROTECTED signal, cross-linked in the frontend per
    design §3.4). The state columns are NOT NULL.
    """
    op.execute(
        """
        CREATE TABLE exit_proximity (
            id BIGSERIAL PRIMARY KEY,
            cycle_ts_utc TIMESTAMPTZ NOT NULL,
            session_date_et DATE NOT NULL,
            market TEXT NOT NULL,
            direction TEXT NOT NULL
                CHECK (direction IN ('long', 'short')),

            -- Snapshot values (held_days NULL when opened_at unknown;
            -- last_close NULL when warming up; stop_price NULL when no working
            -- stop_market order is on record for the market).
            held_days INTEGER,
            last_close NUMERIC,
            stop_price NUMERIC,

            -- (c) trend_flip: deterioration pct (how far close sits from
            -- MA_FAST) + the MIN_HOLDING-days headroom (MIN_HOLDING_DAYS -
            -- held_days; negative once the floor clears).
            trend_flip_state TEXT NOT NULL
                CHECK (trend_flip_state IN ('holding', 'near', 'triggered')),
            trend_flip_headroom NUMERIC,
            held_days_headroom INTEGER,

            -- (a) stop: re-derived api-side from the joined working stop
            -- (Q1 = (B)). NULL headroom + HOLDING state == no stop on record.
            stop_state TEXT NOT NULL
                CHECK (stop_state IN ('holding', 'near', 'triggered')),
            stop_headroom_pct NUMERIC,

            -- (b) reversal (derived from the opposite-direction entry
            -- proximity, ED4) + (d) decommission (binary kill switch) — both
            -- carry state only, no numeric headroom.
            reversal_state TEXT NOT NULL
                CHECK (reversal_state IN ('holding', 'near', 'triggered')),
            decommission_state TEXT NOT NULL
                CHECK (decommission_state IN ('holding', 'near', 'triggered')),

            -- Summary — re-derived AFTER the stop join (combine_overall_exit).
            overall_state TEXT NOT NULL
                CHECK (overall_state IN ('holding', 'near', 'triggered')),
            closest_exit TEXT NOT NULL
                CHECK (closest_exit IN ('stop', 'reversal', 'trend_flip', 'decommission')),
            gate_status TEXT NOT NULL
                CHECK (gate_status IN (
                    'active', 'warming_up', 'min_holding_blocked', 'decommissioned'
                )),

            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Latest-per-market lookups (the GET /api/today/exit-proximity endpoint
    # walks this index via DISTINCT ON market ORDER BY cycle_ts_utc DESC).
    op.execute(
        "CREATE INDEX idx_exit_proximity_market_cycle ON exit_proximity (market, cycle_ts_utc DESC)"
    )

    # Cycle-wide scans (debugging "what positions did the strategy see at T?"
    # and the eod-recon per-cycle row-count check).
    op.execute("CREATE INDEX idx_exit_proximity_cycle_ts ON exit_proximity (cycle_ts_utc DESC)")


def downgrade() -> None:
    """Drop the table + its indexes (indexes drop implicitly with the table)."""
    op.execute("DROP TABLE IF EXISTS exit_proximity")
