"""Create ``strategy_decisions`` + ``strategy_worker_status`` (crypto-pivot C1 strategy worker).

Delta spec §3.3/§3.7 (``Docs/crypto-pivot-delta-spec.md``): the strategy
worker's queryable persistence. One migration, full ``downgrade()`` per
[A16].

**``strategy_decisions``** — one row per (account, UTC decision date).
Written at the 00:05 UTC daily decision (status ``dispatching`` before
the first order leg, ``completed`` after; ``skipped_*`` when the halt
gate or stale bars blocked the decision). Three consumers:

1. **Decision-day dedupe / crash recovery** — the worker starting
   mid-day (or restarting mid-dispatch) reads today's row to decide
   whether to run, resume, or skip. ``UNIQUE (account_id,
   decision_date)`` is the dedupe primitive.
2. **The §3.8 ``/cycle`` Discord digest + §3.7 ``GET /api/system/cycle``**
   (follow-up PRs) read ``outcome`` — per-asset score → target → action
   → costs, Decimals as strings ([A05]).
3. **Loss-limit history** — the risk loop's weekly (rolling-7-day) and
   monthly-drawdown inputs come from the ``equity_usd`` column of prior
   rows (the live analog of the reference engine's trailing
   ``week_equity`` window).

``engine_state`` snapshots the per-asset signal-engine runtime state
(hysteresis counters, lockout, vol-block latch, entry refs, stop levels)
AFTER the decision — the restart-recovery source of truth for state the
venue cannot supply.

**``strategy_worker_status``** — one mutable row per account: the 30 s
risk-loop heartbeat (the §3.7/§3.9 pipeline-freshness input — the
worker runs in its own container, so unlike the api-resident market-data
worker's in-memory status this MUST cross a process boundary; the DB row
is the analog), day-start equity for the daily loss limit, the
weekly-halving window, the current engine state (updated intraday when
stops fire), and the monotonic flatten sequence counter (persisted
BEFORE each risk-loop flatten so the deterministic client_order_id
survives a crash-restart — gate A3).

**Why NUMERIC-as-columns + JSONB-for-shape** — mirrors ``signals``
(``sizing_trace`` JSONB) and ``product_metadata`` (``raw`` JSONB):
first-class columns for what SQL filters on, JSONB for the full
auditable payload.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260709_strategy_worker
Revises: 20260708_crypto_pivot_tables
Create Date: 2026-07-09 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260709_strategy_worker"
down_revision: str | None = "20260709_coinbase_recon_src"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two strategy-worker tables + read indexes."""
    op.execute(
        """
        CREATE TABLE strategy_decisions (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            env TEXT NOT NULL,
            decision_date DATE NOT NULL,
            decided_at_utc TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN (
                    'dispatching','completed','failed',
                    'skipped_risk_state','skipped_stale_bars'
                )),
            equity_usd NUMERIC(20,8),
            equity_for_sizing_usd NUMERIC(20,8),
            v_target NUMERIC(10,6),
            m_combined NUMERIC(10,6),
            weekly_halved BOOLEAN NOT NULL DEFAULT FALSE,
            parameter_set_hash CHAR(64),
            outcome JSONB NOT NULL DEFAULT '{}'::jsonb,
            engine_state JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at_utc TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_strategy_decisions_account_date
                UNIQUE (account_id, decision_date)
        )
        """
    )
    op.execute("CREATE INDEX strategy_decisions_date ON strategy_decisions(decision_date DESC)")
    op.execute(
        """
        CREATE TABLE strategy_worker_status (
            account_id UUID PRIMARY KEY REFERENCES accounts(id),
            env TEXT NOT NULL,
            risk_loop_heartbeat_utc TIMESTAMPTZ,
            risk_loop_tick_count BIGINT NOT NULL DEFAULT 0,
            marks_stale BOOLEAN NOT NULL DEFAULT FALSE,
            day_start_date DATE,
            day_start_equity_usd NUMERIC(20,8),
            weekly_halved_until DATE,
            flatten_seq INTEGER NOT NULL DEFAULT 0,
            engine_state JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_decision_date DATE,
            updated_at_utc TIMESTAMPTZ NOT NULL
        )
        """
    )
    # No explicit GRANTs: 0006_roles' ALTER DEFAULT PRIVILEGES already
    # grants app_service DML on new tables (same as the C0-B1 tables).


def downgrade() -> None:
    """Drop both tables ([A16] rollback contract)."""
    op.execute("DROP TABLE IF EXISTS strategy_worker_status")
    op.execute("DROP TABLE IF EXISTS strategy_decisions")
