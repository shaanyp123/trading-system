"""Allow ``auto_rereconciled`` in ``reconciliation_breaks.resolution_path``.

C1 recon-hardening ride-along (#375 accepted cosmetic, 2026-07-13). The
EOD cycle's natural re-resolution path — a subsequent recon cycle
observes that a previously-detected break no longer diverges and stamps
``resolved_at_utc`` via ``services/reconciliation/apply.py::
_resolve_prior_breaks`` — has always stamped ``resolution_path =
'manual'`` because the alembic-0004 CHECK offered no honest value and
``manual`` was the ``apply_reconciliation_plan`` parameter default. That
misrecords machine resolutions as operator actions (observed live: the
2026-07-11 phantom-break pair auto-resolved by the 2026-07-12 cycle
reads ``manual`` although nobody touched it).

This migration extends the CHECK with ``auto_rereconciled``;
``run_eod_cycle`` now passes it explicitly. The ``manual`` parameter
default is unchanged (conservative for operator tooling), and
HISTORICAL rows are deliberately NOT re-stamped — rewriting past
``resolution_path`` values would guess at intent; the cosmetic fix is
going-forward only.

The mirror Literal lives at
``services/reconciliation/apply.py::ResolutionPathLiteral``; keep them
in sync.

NOTE: the revision id is abbreviated (``alembic_version.version_num``
is VARCHAR(32)) — same convention as ``20260529_recon_src_degraded``.

A02 BINDS — ``alembic/**`` is on the forbidden whitelist.
``risk-review-approved`` required.
A16 — ``downgrade()`` provided (re-stamps ``auto_rereconciled`` rows to
``manual`` — the pre-migration recording — before narrowing the CHECK,
so the downgrade never fails validation and never deletes evidence).

Revision ID: 20260713_recon_resolution_auto
Revises: 20260710_usdc_rewards_capture
Create Date: 2026-07-13 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260713_recon_resolution_auto"
down_revision: str | None = "20260710_usdc_rewards_capture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend the ``resolution_path`` CHECK with ``auto_rereconciled``.

    Postgres auto-names an inline column CHECK ``<table>_<column>_check``
    (here ``reconciliation_breaks_resolution_path_check`` from alembic
    0004). Drop + re-add is the canonical constraint-extension pattern
    (``ALTER CONSTRAINT`` cannot change a CHECK expression); ``IF
    EXISTS`` keeps the drop idempotent on re-runs.
    """
    op.execute(
        "ALTER TABLE reconciliation_breaks "
        "DROP CONSTRAINT IF EXISTS reconciliation_breaks_resolution_path_check"
    )
    op.execute(
        """
        ALTER TABLE reconciliation_breaks
            ADD CONSTRAINT reconciliation_breaks_resolution_path_check
            CHECK (resolution_path IN
                ('grace_period','manual','kill_switch',
                 'tolerance_widened_dividend','auto_rereconciled'))
        """
    )


def downgrade() -> None:
    """Restore the alembic-0004 CHECK (re-stamps rows, deletes nothing).

    ``auto_rereconciled`` rows revert to ``manual`` — exactly what the
    pre-migration code would have recorded — so re-adding the narrower
    CHECK cannot fail validation. The break rows themselves (and the
    hash-chained ``reconciliation_break_resolved`` audit events, the
    durable record) are untouched.
    """
    op.execute(
        "UPDATE reconciliation_breaks SET resolution_path = 'manual' "
        "WHERE resolution_path = 'auto_rereconciled'"
    )
    op.execute(
        "ALTER TABLE reconciliation_breaks "
        "DROP CONSTRAINT IF EXISTS reconciliation_breaks_resolution_path_check"
    )
    op.execute(
        """
        ALTER TABLE reconciliation_breaks
            ADD CONSTRAINT reconciliation_breaks_resolution_path_check
            CHECK (resolution_path IN
                ('grace_period','manual','kill_switch','tolerance_widened_dividend'))
        """
    )
