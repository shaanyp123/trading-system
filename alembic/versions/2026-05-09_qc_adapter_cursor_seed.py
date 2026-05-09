"""Idempotent re-seed of the qc_adapter_cursor table.

Backend-spec §3.19 specifies three rows seeded into ``qc_adapter_cursor`` at
deployment::

    INSERT INTO qc_adapter_cursor VALUES
      ('/events/', 0, now(), 0, 0),
      ('/state/portfolio.json', 0, now(), 0, 0),
      ('/instruction_acks/', 0, now(), 0, 0);

These rows are ALREADY inserted by ``alembic/versions/0004_ops_tables.py``
at table-creation time (see lines 143-153 of that file). This migration is
a defensive idempotent re-seed that:

* runs ``INSERT ... ON CONFLICT (directory_path) DO NOTHING`` so it is a
  no-op against a current, healthy schema;
* protects against any future deploy that may have run 0004 before its
  INSERT block landed (e.g., if a fix-forward ever rewrote 0004 to drop
  the inline INSERT and add it as a separate seed, the cursor table
  would be empty until this migration runs);
* is also a useful smoke-check at upgrade time — if the cursor table is
  missing for any reason, the INSERT here will fail loudly rather than
  letting the qc_adapter come up against a broken schema.

The downgrade is INTENTIONALLY a no-op — issuing
``DELETE FROM qc_adapter_cursor WHERE directory_path IN (...)`` would
delete the rows owned by 0004 and break the qc_adapter on a downgrade-
then-reupgrade cycle. Since the upgrade is itself idempotent and a no-op
on a healthy schema, the downgrade has nothing to undo.

Filename follows the operational dating scheme from dev-guide §7.1
(``YYYY-MM-DD_<short>.py``). The Day 3 foundational migrations 0001-0006
are sealed; this is the first operational migration.

Revision ID: 20260509_qc_adapter_cursor_seed
Revises: 0006_roles
Create Date: 2026-05-09 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260509_qc_adapter_cursor_seed"
down_revision: str | None = "0006_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SEED_DIRECTORY_PATHS: tuple[str, ...] = (
    "/events/",
    "/state/portfolio.json",
    "/instruction_acks/",
)


def upgrade() -> None:
    """Re-seed any missing canonical rows in qc_adapter_cursor.

    Idempotent: ``ON CONFLICT (directory_path) DO NOTHING`` lets this run
    safely against a schema where 0004 already inserted the rows.
    """
    op.execute(
        """
        INSERT INTO qc_adapter_cursor
            (directory_path, last_consumed_sequence_no, last_consumed_at_utc,
             bytes_consumed, consecutive_failures)
        VALUES
            ('/events/', 0, now(), 0, 0),
            ('/state/portfolio.json', 0, now(), 0, 0),
            ('/instruction_acks/', 0, now(), 0, 0)
        ON CONFLICT (directory_path) DO NOTHING
        """
    )


def downgrade() -> None:
    """Intentional no-op.

    The three canonical seed rows are owned by ``0004_ops_tables.py``,
    which inserts them at table creation. A DELETE here would corrupt
    the cursor on a downgrade-then-reupgrade cycle by removing rows
    0004 expects to be present.

    See module docstring for the full rationale.
    """
    # Marker reference so the tuple isn't reported as unused by linters
    # while still keeping the canonical list in one place.
    _ = _SEED_DIRECTORY_PATHS
