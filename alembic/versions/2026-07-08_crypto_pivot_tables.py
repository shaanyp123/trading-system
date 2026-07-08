"""Create ``funding_rates`` + ``cash_sweeps`` + ``product_metadata`` (crypto-pivot C0-B1).

Delta spec §3.7 (``Docs/crypto-pivot-delta-spec.md``, APPROVED WITH OPERATOR
MODIFICATIONS 2026-07-08): the three new tables the Coinbase CFM build
writes from day one. One migration, full ``downgrade()`` per [A16].

**``funding_rates``** — hourly CDE funding snapshots per product (§3.2
"funding logger from day one", REPORT F-3 rider). The nightly comparison
of realized funding vs the parametric model (10.95%/yr) feeds live gate
B3, which runs permanently (decisions-log 2026-07-08 refinement
governance). Append-only; ``UNIQUE (product_id, observed_at_utc)`` makes
the hourly ingest idempotent across restarts (re-observing the same hour
upserts nothing). ``rate_per_interval`` is the venue's raw per-interval
rate; ``interval_hours`` records the venue's settlement cadence at
observation time so annualization is derivable without assuming the
cadence never changes. NUMERIC throughout ([A05] — never float for money
or rates).

**``cash_sweeps``** — one row per §3.6 cash-yield sweep (both directions).
The cash manager (``services/risk/cash_manager.py``, C2 activation) writes
a row alongside the EXISTING ``capital_event`` audit taxonomy — the audit
chain stays authoritative for "what happened"; this table is the queryable
operational ledger ("what is currently swept"). ``audit_event_uuid`` links
the two. ``direction`` CHECK: ``to_yield`` (USD → USDC rewards balance) |
``to_margin`` (reclaim; must be same-day per the locked decision).
``status`` CHECK: ``requested`` → ``completed`` | ``failed`` — a sweep
that fails midway must be visible, never silently retried into ambiguity.

**``product_metadata``** — daily snapshot of the tradable CDE product
specs (§3.7: tick size, contract size, margin requirement — refreshed
daily). Append-only snapshots (not upserts) so the REPORT-rider
auto-demotion check ("fee/margin change detection") can diff today vs
yesterday from this table alone. ``raw`` JSONB carries the full venue
payload for forensic diffing beyond the first-class columns. Product IDs
are discovered at runtime and never hardcoded ([A13] revised) — hence
TEXT, no enum.

**Why no FK to ``accounts``** — same rationale as ``signal_proximity``
(2026-05-28): all three tables are scoped to the venue/product surface,
not a particular account. Single-operator Phase C has one account; a
multi-account future can add the column without invalidating rows.

**Why BIGSERIAL not UUID** — append-only, never FK-referenced (except
``cash_sweeps.audit_event_uuid`` pointing OUT to the audit chain);
matches the ``signal_proximity``/``exit_proximity`` precedent.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260708_crypto_pivot_tables
Revises: 20260608_order_placement_failed
Create Date: 2026-07-08 20:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260708_crypto_pivot_tables"
down_revision: str | None = "20260608_order_placement_failed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the three crypto-pivot tables + their read indexes."""
    op.execute(
        """
        CREATE TABLE funding_rates (
            id BIGSERIAL PRIMARY KEY,
            product_id TEXT NOT NULL,
            observed_at_utc TIMESTAMPTZ NOT NULL,
            rate_per_interval NUMERIC NOT NULL,
            interval_hours NUMERIC NOT NULL,
            mark_price NUMERIC,
            source TEXT NOT NULL DEFAULT 'coinbase_advanced',
            created_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_funding_rates_product_observed
                UNIQUE (product_id, observed_at_utc)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_funding_rates_product_observed_desc
            ON funding_rates (product_id, observed_at_utc DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE cash_sweeps (
            id BIGSERIAL PRIMARY KEY,
            requested_at_utc TIMESTAMPTZ NOT NULL,
            completed_at_utc TIMESTAMPTZ,
            direction TEXT NOT NULL
                CHECK (direction IN ('to_yield', 'to_margin')),
            amount_usd NUMERIC NOT NULL CHECK (amount_usd > 0),
            status TEXT NOT NULL DEFAULT 'requested'
                CHECK (status IN ('requested', 'completed', 'failed')),
            equity_usd_at_request NUMERIC,
            margin_reserve_usd_at_request NUMERIC,
            reason TEXT NOT NULL,
            audit_event_uuid UUID,
            failure_detail TEXT,
            created_at_utc TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_cash_sweeps_requested_desc
            ON cash_sweeps (requested_at_utc DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_cash_sweeps_status
            ON cash_sweeps (status)
            WHERE status <> 'completed'
        """
    )

    op.execute(
        """
        CREATE TABLE product_metadata (
            id BIGSERIAL PRIMARY KEY,
            product_id TEXT NOT NULL,
            captured_at_utc TIMESTAMPTZ NOT NULL,
            tick_size NUMERIC,
            contract_size NUMERIC,
            initial_margin_requirement NUMERIC,
            maintenance_margin_requirement NUMERIC,
            taker_fee_bps NUMERIC,
            maker_fee_bps NUMERIC,
            trading_disabled BOOLEAN,
            raw JSONB NOT NULL,
            created_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_product_metadata_product_captured
                UNIQUE (product_id, captured_at_utc)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_product_metadata_product_captured_desc
            ON product_metadata (product_id, captured_at_utc DESC)
        """
    )


def downgrade() -> None:
    """Drop the three crypto-pivot tables (reverse creation order)."""
    op.execute("DROP TABLE IF EXISTS product_metadata")
    op.execute("DROP TABLE IF EXISTS cash_sweeps")
    op.execute("DROP TABLE IF EXISTS funding_rates")
