"""Create ``signal_proximity`` table for the V1 /signals "Watching" view (PR-B).

PR-B of ``Docs/signal-proximity-design.md`` (SIGNED OFF 2026-05-28). Stores
the per-market gate-proximity records LEAN emits on every daily
``lean_cycle_heartbeat`` (21:30 UTC). Each row is the snapshot of one
market's three gates (Donchian / Trend / Hurst) plus the overall summary.

**Why a dedicated table and not JSONB on ``liveness_probes``** — D4 of the
design doc: the proximity payload is queryable / indexable / orthogonal to
the liveness heartbeat. Mixing them onto ``liveness_probes`` would couple
the two surfaces and complicate downstream reads.

**Why no audit_log row gated before write** — Q2 of the design doc
(re-confirmed by the operator 2026-05-28): proximity rows are observational
(LEAN reporting what it saw on a daily evaluation), NOT a state change.
The table IS the durable audit trail for the daily evaluation. The
audit-first ordering rule (``feedback_audit_first_ordering`` memory) is
specifically for state-change events; this carve-out is explicit and
locked.

**Retention** — keep all rows. Volume = ~10 markets x 1 cycle/day = ~10
rows/day. Storage cost is negligible.

**CHECK-constraint values** — pinned to the Python enums in
``strategies/v1_trend_following/proximity.py`` so a future addition (e.g.,
a new gate state) is caught by a migration mismatch rather than silently
accepted:

  * per-gate state columns → ``GateState`` ('pass' | 'close' | 'fail')
  * ``overall_state``       → ``GateState`` ('pass' | 'close' | 'fail')
  * ``closest_gate``        → 'donchian' | 'trend' | 'hurst' | 'history'
  * ``gate_status``         → ``GateStatus`` ('ok' | 'warming_up' | 'decommissioned')

The ``gate_status`` CHECK is the one called out in the PR-A risk-review
carry-over — restricting it to the exact ``GateStatus`` literal in
``proximity.py`` (line 72) prevents accidental drift.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260528_signal_proximity
Revises: 20260527_position_unprotected
Create Date: 2026-05-28 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260528_signal_proximity"
down_revision: str | None = "20260527_position_unprotected"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``signal_proximity`` table + its two read indexes.

    No FK to ``accounts`` — proximity rows are scoped to the algorithm /
    universe, not a particular account. Single-operator Phase 1 already
    has one account; a future multi-account expansion can add the FK
    without invalidating the existing rows.

    ``id`` is ``BIGSERIAL`` (not ``UUID``) because the table is
    append-only and never referenced by FK; ``BIGSERIAL`` is sufficient
    and saves the ``uuid_generate_v7()`` call per row.

    All ``NUMERIC`` columns are NULLable to support the warming-up case
    (per ``proximity.compute_market_proximity``'s warming-up branch — see
    ``strategies/v1_trend_following/proximity.py`` line 230). The state
    columns are NOT NULL and carry the sentinel ``'fail'`` value in that
    branch; ``gate_status='warming_up'`` is the discriminator.
    """
    op.execute(
        """
        CREATE TABLE signal_proximity (
            id BIGSERIAL PRIMARY KEY,
            cycle_ts_utc TIMESTAMPTZ NOT NULL,
            session_date_et DATE NOT NULL,
            market TEXT NOT NULL,

            -- Raw snapshot values (NULL when warming up).
            last_close NUMERIC,
            donchian_high NUMERIC,
            donchian_low NUMERIC,
            ma_fast NUMERIC,
            ma_slow NUMERIC,
            hurst_value NUMERIC,
            hurst_threshold NUMERIC,
            atr NUMERIC,

            -- Per-gate state + headroom (state NOT NULL; headroom NULL
            -- when warming up).
            long_donchian_state TEXT NOT NULL
                CHECK (long_donchian_state IN ('pass', 'close', 'fail')),
            long_donchian_headroom_pct NUMERIC,
            short_donchian_state TEXT NOT NULL
                CHECK (short_donchian_state IN ('pass', 'close', 'fail')),
            short_donchian_headroom_pct NUMERIC,
            long_trend_state TEXT NOT NULL
                CHECK (long_trend_state IN ('pass', 'close', 'fail')),
            long_trend_gap_pct NUMERIC,
            short_trend_state TEXT NOT NULL
                CHECK (short_trend_state IN ('pass', 'close', 'fail')),
            short_trend_gap_pct NUMERIC,
            hurst_state TEXT NOT NULL
                CHECK (hurst_state IN ('pass', 'close', 'fail')),
            hurst_headroom NUMERIC,

            -- Overall summary.
            overall_state TEXT NOT NULL
                CHECK (overall_state IN ('pass', 'close', 'fail')),
            closest_gate TEXT NOT NULL
                CHECK (closest_gate IN ('donchian', 'trend', 'hurst', 'history')),
            gate_status TEXT NOT NULL
                CHECK (gate_status IN ('ok', 'warming_up', 'decommissioned')),

            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Latest-per-market lookups (the GET /api/signals/proximity endpoint
    # walks this index via DISTINCT ON market ORDER BY cycle_ts_utc DESC).
    op.execute(
        "CREATE INDEX idx_signal_proximity_market_cycle "
        "ON signal_proximity (market, cycle_ts_utc DESC)"
    )

    # Cycle-wide scans (debugging "what did the strategy see at T?" and
    # the eod-recon ceremony's per-cycle row-count check).
    op.execute("CREATE INDEX idx_signal_proximity_cycle_ts ON signal_proximity (cycle_ts_utc DESC)")


def downgrade() -> None:
    """Drop the table + its indexes (indexes drop implicitly with the table)."""
    op.execute("DROP TABLE IF EXISTS signal_proximity")
