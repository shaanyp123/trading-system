"""Create ``usdc_reward_transactions`` + ``cash_balance_snapshots`` (USDC-interest capture).

Operator feature request (decisions-log 2026-07-09/-10, "measure don't
estimate"): capture the Coinbase One USDC rewards the operator's idle
CBI USDC balance earns, plus a daily snapshot of the cash balances the
venue's auto-sweep semantics make load-bearing. Written by the daily
``services/data/usdc_rewards.py`` capture job (00:20 UTC, after the
00:15 recon); read by ``GET /api/system/funding`` for the Today-page
funding + cash-yield strip.

**``usdc_reward_transactions``** — one row per persisted Coinbase v2
ledger transaction from the operator's USDC account. The capture filter
is DELIBERATELY LOOSE at build time: the venue's reward transaction
``type`` is unknown until the first weekly payout is observed (rewards
pay out every Friday — operator app screenshot 2026-07-09), so the
poller persists EVERY transaction whose type is not in the observed
conversion/transfer set (buy/sell/send) and logs unfamiliar types; the
filter locks to the real reward type in a follow-up once observed.
Hence ``transaction_type`` is TEXT with no CHECK — constraining it now
would guess at venue truth. ``raw`` JSONB carries the full v2 payload
for that observation. ``UNIQUE (venue_transaction_id)`` (the v2 ledger
id) makes the daily re-poll idempotent (``ON CONFLICT DO NOTHING``).
``amount`` is SIGNED (a reward clawback must be visible, never
silently dropped); reward aggregation sums positive amounts only.

**``cash_balance_snapshots``** — one row per UTC calendar day: the CBI
spot USD + USDC ``available`` balances from ``get_accounts`` at capture
time. Records the two balances the venue's auto-sweep semantics make
operationally significant (decisions-log entry riding this PR): spot
USD IS trading equity (the venue sweeps it in/out of derivatives
margin); CBI USDC is INVISIBLE to equity. ``UNIQUE (snapshot_date_utc)``
+ ``DO NOTHING`` = first capture of the day wins (a mid-day redeploy
re-fire never overwrites the 00:20 UTC observation). DATE not
TIMESTAMPTZ for the key — the unit is a UTC calendar day, same
precedent as ``risk_state.convalescent_last_counted_day_utc``
(20260710_conv_clean_day); ``captured_at_utc`` carries the exact
observation instant. Distinct from the existing ``balances`` table by
design: ``balances`` is the recon EOD NLV of the FUTURES complex
(account-scoped, venue ``futures_balance_summary``); this table is the
spot/CBI cash side that summary does NOT decompose.

**Why no FK to ``accounts``** — same rationale as the crypto-pivot
tables (20260708): both tables are scoped to the venue cash surface,
not a particular backend account row. **Why BIGSERIAL not UUID** —
append-only, never FK-referenced; matches funding_rates precedent.
NUMERIC throughout ([A05] — never float for money).

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.

Revision ID: 20260710_usdc_rewards_capture
Revises: 20260710_conv_clean_day
Create Date: 2026-07-10 02:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260710_usdc_rewards_capture"
down_revision: str | None = "20260710_conv_clean_day"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two USDC-interest capture tables + read indexes."""
    op.execute(
        """
        CREATE TABLE usdc_reward_transactions (
            id BIGSERIAL PRIMARY KEY,
            venue_transaction_id TEXT NOT NULL,
            account_currency TEXT NOT NULL DEFAULT 'USDC',
            transaction_type TEXT NOT NULL,
            transaction_status TEXT,
            amount NUMERIC NOT NULL,
            currency TEXT NOT NULL,
            native_amount NUMERIC,
            native_currency TEXT,
            description TEXT,
            venue_created_at_utc TIMESTAMPTZ NOT NULL,
            raw JSONB NOT NULL,
            created_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_usdc_reward_tx_venue_id
                UNIQUE (venue_transaction_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_usdc_reward_tx_venue_created_desc
            ON usdc_reward_transactions (venue_created_at_utc DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE cash_balance_snapshots (
            id BIGSERIAL PRIMARY KEY,
            snapshot_date_utc DATE NOT NULL,
            captured_at_utc TIMESTAMPTZ NOT NULL,
            spot_usd NUMERIC,
            cbi_usdc NUMERIC,
            raw JSONB NOT NULL,
            created_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_cash_balance_snapshots_date
                UNIQUE (snapshot_date_utc)
        )
        """
    )


def downgrade() -> None:
    """Drop the two capture tables (reverse creation order)."""
    op.execute("DROP TABLE IF EXISTS cash_balance_snapshots")
    op.execute("DROP TABLE IF EXISTS usdc_reward_transactions")
