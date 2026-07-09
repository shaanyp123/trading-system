"""Allow ``coinbase_eod`` in the ``balances.source`` CHECK constraint.

Crypto-pivot C0 §3.5 (2026-07-09). The Coinbase EOD recon fetcher
(``services/reconciliation/coinbase_fetcher.py``) replaces the deleted
IBKR FlexQuery path; every recon cycle INSERTs one ``balances`` row
stamped ``source = 'coinbase_eod'``
(``services/reconciliation/eod_cycle.py::BALANCE_SOURCE_FROM_COINBASE``,
mirrored by ``services/api/repos/phase1.py::_RECON_BALANCE_SOURCE`` for
the /system reconciliation tile's freshness signal). Without this
migration that INSERT violates the alembic-0002 CHECK
(``qc_objectstore | tws_api | flexquery_eod``) and the recon cycle can
never persist a snapshot.

The legacy values stay in the constraint: ``tws_api`` is still written
by ``services/risk/fill_processor.py`` (its Coinbase re-feed keeps the
same source string until the C1 fill-wiring PR revisits it), and the
historical values keep old rows valid on any database that predates
the 2026-07-09 fresh-start bringup.

``positions_history`` / ``pdt_day_trade_log`` carry the same CHECK but
have no writer in the crypto stack — their constraints are left
untouched until a consumer needs them (smallest-diff rule).

The mirror enum member lives at
``services/reconciliation/recon.py::BrokerSource.COINBASE_EOD``.

NOTE: the revision id is abbreviated (``alembic_version.version_num``
is VARCHAR(32)) — same convention as ``20260529_recon_src_degraded``.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.
A16 — ``downgrade()`` provided (restores the pre-pivot value set;
requires no ``coinbase_eod`` rows remain, which the guard DELETE makes
explicit rather than leaving a constraint-violation surprise).

Revision ID: 20260709_coinbase_recon_src
Revises: 20260708_crypto_pivot_tables
Create Date: 2026-07-09 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260709_coinbase_recon_src"
down_revision: str | None = "20260708_crypto_pivot_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend ``balances.source`` CHECK with ``coinbase_eod``.

    Postgres auto-names an inline column CHECK ``<table>_<column>_check``
    (here ``balances_source_check`` from alembic 0002). Drop + re-add is
    the canonical constraint-extension pattern (``ALTER CONSTRAINT``
    cannot change a CHECK expression). ``IF EXISTS`` keeps the drop
    idempotent on re-runs.
    """
    op.execute("ALTER TABLE balances DROP CONSTRAINT IF EXISTS balances_source_check")
    op.execute(
        """
        ALTER TABLE balances ADD CONSTRAINT balances_source_check
            CHECK (source IN ('qc_objectstore','tws_api','flexquery_eod','coinbase_eod'))
        """
    )


def downgrade() -> None:
    """Restore the pre-pivot CHECK (drops ``coinbase_eod`` rows first).

    The DELETE is deliberate and explicit: re-adding the narrower CHECK
    with ``coinbase_eod`` rows present would fail validation. Recon
    balance snapshots are derivative state (re-creatable from the next
    cycle); the hash-chained audit log — the durable record — is
    untouched.
    """
    op.execute("DELETE FROM balances WHERE source = 'coinbase_eod'")
    op.execute("ALTER TABLE balances DROP CONSTRAINT IF EXISTS balances_source_check")
    op.execute(
        """
        ALTER TABLE balances ADD CONSTRAINT balances_source_check
            CHECK (source IN ('qc_objectstore','tws_api','flexquery_eod'))
        """
    )
