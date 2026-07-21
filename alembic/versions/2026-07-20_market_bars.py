"""Create ``market_bars`` — persistent OHLCV (incl. volume) bar store.

Operator directive 2026-07-20 (decisions-log "agentic-refinement data
capture"): the strategy-refinement audit found daily candles and VOLUME
were fetched fresh each cycle and discarded — the v2.1 volume study's
out-of-sample plan depends on live-period bars we were not recording.
This table is the durable record: daily + hourly bars for the spot
signal products (``BTC-USD``/``ETH-USD``) and the discovered CDE perp
products on the traded base assets, written by the market-data worker
(`services/data/coinbase_market_data.py`) and the operator backfill
script (`scripts/operator_tools/backfill_market_bars.py`).

Shape notes:
* ``bar_start_utc`` is the venue's bar-START timestamp (candles API
  convention); a daily bar for session 2026-07-19 has bar_start_utc
  2026-07-19T00:00:00Z.
* ``granularity`` mirrors the venue enum values used (`ONE_DAY` /
  `ONE_HOUR`) rather than inventing a local vocabulary.
* ``volume`` is NULLABLE: spot candles always carry it, but a venue
  payload with a missing/garbage volume should not cost us the price
  columns. Spot volume is base-asset units; perp volume is contracts —
  consumers must key off product_id/source, never mix.
* Writes are ``ON CONFLICT DO NOTHING``: a closed bar is immutable, the
  first capture wins, and the daily/hourly jobs re-write trailing
  windows so gaps self-heal without churning existing rows.
* No FKs — product_ids include expiring CDE codes discovered at runtime
  ([A13]); the bar record must outlive product rotation.

Read side: the analysis/refinement surfaces query by (product_id,
granularity, bar_start_utc range); PK covers it, plus a global
time-ordered index for cross-product windows.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist;
``risk-review-approved`` required. Full ``downgrade()`` per [A16].

Revision ID: 20260720_market_bars
Revises: 20260719_day_start_captured_at
Create Date: 2026-07-20 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260720_market_bars"
down_revision: str | None = "20260719_day_start_captured_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create market_bars + the cross-product time index."""
    op.execute(
        """
        CREATE TABLE market_bars (
            product_id TEXT NOT NULL,
            granularity TEXT NOT NULL
                CHECK (granularity IN ('ONE_DAY', 'ONE_HOUR')),
            bar_start_utc TIMESTAMPTZ NOT NULL,
            open NUMERIC(20,8) NOT NULL,
            high NUMERIC(20,8) NOT NULL,
            low NUMERIC(20,8) NOT NULL,
            close NUMERIC(20,8) NOT NULL,
            volume NUMERIC(30,10),
            source TEXT NOT NULL DEFAULT 'coinbase_advanced',
            captured_at_utc TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (product_id, granularity, bar_start_utc)
        )
        """
    )
    op.execute("CREATE INDEX market_bars_start ON market_bars(bar_start_utc DESC)")
    # No explicit GRANTs: 0006_roles' ALTER DEFAULT PRIVILEGES already
    # grants app_service DML on new tables (same as the C0-B1 tables).


def downgrade() -> None:
    """Drop the table ([A16] rollback contract)."""
    op.execute("DROP TABLE IF EXISTS market_bars")
