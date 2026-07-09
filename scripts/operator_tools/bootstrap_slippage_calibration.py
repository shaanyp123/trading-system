"""scripts/operator_tools/bootstrap_slippage_calibration.py — idempotent seed of
the ``slippage_calibration_versions`` HEAD row (zero-prior bootstrap).

Unblocks C1 strategy-worker start on a fresh DB: ``signals`` rows FK into
``slippage_calibration_versions`` (alembic 0003), so on a fresh database the
worker fails closed with ``strategy_worker_no_slippage_head_fail_closed`` and
will NOT dispatch until a HEAD calibration row exists
(``deploy/strategy_worker/README.md`` prerequisite 3). This tool is that
prerequisite's operator ceremony.

**What it writes (wet run, fresh DB):** exactly one
``slippage_calibration_recalibrated`` audit event + one
``slippage_calibration_versions`` row (``version_no=1``, ``is_head=TRUE``,
``trigger='bootstrap'``, empty ``per_market_coefficients`` — the zero-prior
Phase-1 profile per backend-spec §2.14 "Bootstrap").

**Ceremony — the :class:`services.calibration.calibration.CalibrationPlan`
persistence contract, verbatim:**

  1. ``append_audit_event(...)`` for ``plan.audit_event`` FIRST; capture the
     returned ``audit_event_uuid`` (the writer owns its own SERIALIZABLE +
     advisory-lock transaction).
  2. INSERT the ``slippage_calibration_versions`` row referencing that uuid
     (the ``audit_event_uuid`` column is NOT NULL — the audit write MUST
     precede the INSERT).
  3. HEAD-pointer swap (``SET is_head = FALSE WHERE is_head`` then
     ``SET is_head = TRUE WHERE id = :new_version_id``) in ONE transaction
     WITH the INSERT of step 2; the ``slippage_head`` partial unique index
     enforces the single-HEAD invariant. The audit write stays in its own
     earlier transaction — if step 2/3 fails after step 1, an audit row exists
     for a version that did not land, which is the correct audit-first
     behavior (audit records intent; a failed apply is diagnostically
     visible and the re-run mints a fresh version id).

**Idempotency.** If a HEAD row already exists the tool logs
``bootstrap_slippage_calibration_head_exists`` (with its id + version_no) and
exits 0 WITHOUT writing anything — safe to re-run. ``--force-new-version``
overrides that guard and appends a successor zero-prior version parented on
the current HEAD (``version_no = max + 1``), swapping the HEAD pointer to it.

**Policy vs I/O split.** All calibration math/payload shaping stays in the
pure planner ``services.calibration.calibration.plan_calibration_fit``
(trigger=BOOTSTRAP, fills=()); this tool is only the dispatcher I/O the
planner's docstring specifies. NO ``services/**`` module is modified.

**Forbidden-paths check.** ``scripts/operator_tools/**`` is NOT on the
dev-guide §11 anti-pattern [A02] forbidden-modification whitelist. The tool
CALLS ``services/calibration`` (pure planner) + ``services/audit`` (writer)
but modifies neither — regular PR review applies (same posture as
``apply_parameter_change.py``).

**A-gates:**

* **A01 enforced** — the audit row goes through
  ``services.audit.writer.append_audit_event``; no direct ``audit_log``
  INSERT.
* **A04 N/A-safe** — ``slippage_calibration_recalibrated`` is already in the
  locked taxonomy (``services/audit/event_types.py``); no new event type.
* **A05 enforced** — the planner serializes every Decimal as a string in the
  audit payload; the JSONB column stores that same str-Decimal shape
  byte-identically (audit-to-row reconstruction stays trivial).
* **A06 enforced** — every datetime tz-aware UTC.
* **A22 N/A** — unit tests use a stub session factory + a monkeypatched
  writer; no real audit events are emitted by tests.

Exit codes (load-bearing for the operator-runbook contract):

* ``0``  — success (row seeded, HEAD already present no-op, or dry-run plan)
* ``4``  — no active ``accounts`` row (run ``bootstrap_live_account.py`` first;
  the audit event needs an ``account_id`` FK)
* ``5``  — DB init failure (DATABASE_URL missing / wrong / unreachable)
* ``6``  — invalid CLI args (live env without --allow-non-paper;
  --no-dry-run without --confirm)
* ``99`` — unexpected exception (logged with traceback)

Usage::

    # Dry-run (default) — prints the fresh-DB plan, no DB access, no writes:
    python -m scripts.operator_tools.bootstrap_slippage_calibration --env paper

    # Wet — seed the bootstrap HEAD row (two-flag gate):
    python -m scripts.operator_tools.bootstrap_slippage_calibration \\
        --env paper --no-dry-run --confirm

    # Append a successor zero-prior version on top of an existing HEAD:
    python -m scripts.operator_tools.bootstrap_slippage_calibration \\
        --env paper --force-new-version --no-dry-run --confirm

On the VPS, run inside the api container with the in-container DATABASE_URL
wrapper from ``deploy/crypto-vps-bringup.md`` Step 7 (see
``deploy/strategy_worker/README.md`` prerequisite 3 for the exact command).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

import structlog
import uuid_utils as uuid7_lib
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.calibration.calibration import (
    CalibrationPlan,
    CalibrationTrigger,
    plan_calibration_fit,
)

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit-code contract (locked; documented in module docstring)
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_NO_ACCOUNT: Final[int] = 4
EXIT_DB_INIT_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

#: Env values accepted on the CLI. Mirrors ``audit_log.env`` CHECK constraint.
_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

#: DB connection env var. Mirrors the other operator_tools scripts.
DATABASE_URL_ENV: Final[str] = "DATABASE_URL"


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests use this surface directly without going
    through argparse so they don't have to fight stderr capture.
    """

    env: EnvName
    notes: str | None
    force_new_version: bool
    dry_run: bool
    confirm: bool
    allow_non_paper: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.bootstrap_slippage_calibration",
        description=(
            "Idempotent seed of the slippage_calibration_versions HEAD row "
            "(zero-prior bootstrap; trigger='bootstrap'). Unblocks the "
            "strategy worker's fail-closed no-slippage-head gate on a fresh "
            "DB. Re-runs are safe (HEAD already present = no-op)."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Audit env tag — mirrors audit_log.env CHECK. Live envs require "
            "the --allow-non-paper safety gate."
        ),
    )
    parser.add_argument(
        "--notes",
        default=None,
        help=(
            "Optional free-form operational note persisted to the row's "
            "notes column + the audit-event payload."
        ),
    )
    parser.add_argument(
        "--force-new-version",
        action="store_true",
        default=False,
        help=(
            "Append a successor zero-prior version even when a HEAD already "
            "exists (parented on the current HEAD; version_no = max + 1; the "
            "HEAD pointer swaps to the new row). Without this flag an "
            "existing HEAD makes the tool a no-op."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "If set (default), print the fresh-DB bootstrap plan without any "
            "DB access. Operator must pass --no-dry-run AND --confirm to "
            "actually write."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "Explicit confirmation for --no-dry-run. Two-flag gate per the "
            "bootstrap_live_account.py convention. Fails closed otherwise."
        ),
    )
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        help=(
            "Required to run with --env in {live-small, live-scale}. The "
            "script mutates the live database; operator must opt in "
            "explicitly to acknowledge."
        ),
    )
    return parser


def parse_args(argv: list[str]) -> ParsedArgs:
    parsed = _build_parser().parse_args(argv)
    if parsed.env != "paper" and not parsed.allow_non_paper:
        raise ValueError(
            f"--env={parsed.env} requires --allow-non-paper to acknowledge the live-DB mutation"
        )
    if not parsed.dry_run and not parsed.confirm:
        raise ValueError(
            "--no-dry-run requires --confirm. Two-flag gate prevents an accidental wet run."
        )
    return ParsedArgs(
        env=parsed.env,
        notes=parsed.notes,
        force_new_version=bool(parsed.force_new_version),
        dry_run=bool(parsed.dry_run),
        confirm=bool(parsed.confirm),
        allow_non_paper=bool(parsed.allow_non_paper),
    )


# ---------------------------------------------------------------------------
# Pure helpers (testable without a DB)
# ---------------------------------------------------------------------------


def mint_version_id() -> UUID:
    """Fresh UUIDv7 for the new ``slippage_calibration_versions.id``.

    Same minting pattern as ``services.audit.writer.append_audit_event``
    (``uuid_utils.uuid7()`` coerced through the stdlib UUID type). The
    planner deliberately does NOT mint ids (plan-then-apply contract), so
    the caller boundary — this tool — owns generation.
    """
    return UUID(str(uuid7_lib.uuid7()))


def build_bootstrap_plan(
    *,
    new_version_id: UUID,
    calibrated_at_utc: datetime,
    parent_version_id: UUID | None = None,
    parent_version_no: int | None = None,
    notes: str | None = None,
) -> CalibrationPlan:
    """The zero-prior bootstrap plan: trigger=BOOTSTRAP, no fills, becomes HEAD.

    Pure — delegates every policy decision to
    :func:`services.calibration.calibration.plan_calibration_fit`. With
    ``fills=()`` the planner emits empty ``per_market_coefficients`` and
    ``fit_method=zero_prior`` (backend-spec §2.14 "Bootstrap" row).
    """
    return plan_calibration_fit(
        new_version_id=new_version_id,
        trigger=CalibrationTrigger.BOOTSTRAP,
        calibrated_at_utc=calibrated_at_utc,
        fills=(),
        parent_version_id=parent_version_id,
        parent_version_no=parent_version_no,
        becomes_head=True,
        notes=notes,
    )


def coefficients_jsonb(plan: CalibrationPlan) -> str:
    """JSONB text for the ``per_market_coefficients`` column.

    Taken from the plan's audit-event payload so the row's JSONB and the
    audit payload stay byte-identical (Decimal already serialized as string
    by the planner per A05; the chain verifier can reconstruct the row from
    the audit event). Empty ``{}`` for the no-fills bootstrap.
    """
    payload_coefficients: Any = plan.audit_event.payload["per_market_coefficients"]
    return json.dumps(payload_coefficients, sort_keys=True)


# ---------------------------------------------------------------------------
# DB reads + the INSERT + HEAD-swap transaction
# ---------------------------------------------------------------------------


async def _fetch_current_head(
    session_factory: async_sessionmaker[Any],
) -> tuple[UUID, int] | None:
    """Return (id, version_no) of the current HEAD row, or None on a fresh DB."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, version_no FROM slippage_calibration_versions "
                    "WHERE is_head = TRUE LIMIT 1"
                )
            )
        ).fetchone()
    if row is None:
        return None
    return UUID(str(row.id)), int(row.version_no)


async def _fetch_max_version_no(session_factory: async_sessionmaker[Any]) -> int | None:
    """Highest existing ``version_no`` (UNIQUE column), or None when the table
    is empty. The planner's ``version_no = parent + 1`` contract makes the
    caller own this read; using MAX (not the HEAD's version_no) keeps the
    successor collision-free even if non-HEAD research rows ever exist.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT MAX(version_no) AS max_no FROM slippage_calibration_versions")
            )
        ).fetchone()
    if row is None or row.max_no is None:
        return None
    return int(row.max_no)


async def _resolve_active_account_id(session_factory: async_sessionmaker[Any]) -> UUID | None:
    """Active account for the audit event's ``account_id`` FK (same lookup as
    ``apply_parameter_change.py``)."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM accounts WHERE active_to IS NULL "
                    "ORDER BY active_from DESC LIMIT 1"
                )
            )
        ).fetchone()
    return UUID(str(row.id)) if row is not None else None


async def _insert_version_and_swap_head(
    session_factory: async_sessionmaker[Any],
    *,
    plan: CalibrationPlan,
    audit_event_uuid: UUID,
) -> None:
    """CalibrationPlan contract steps 2+3: INSERT the version row, then swap
    the HEAD pointer — all inside ONE transaction (the audit write from step 1
    already committed separately). The ``slippage_head`` partial unique index
    guarantees a partial swap can't leave two HEADs.
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO slippage_calibration_versions ("
                    "    id, version_no, is_head, calibrated_at_utc, trigger, "
                    "    per_market_coefficients, data_window_start, "
                    "    data_window_end, audit_event_uuid, notes"
                    ") VALUES ("
                    "    :id, :version_no, FALSE, :calibrated_at_utc, :trigger, "
                    "    CAST(:coefficients AS JSONB), :window_start, "
                    "    :window_end, :audit_event_uuid, :notes"
                    ")"
                ),
                {
                    "id": plan.new_version_id,
                    "version_no": plan.version_no,
                    "calibrated_at_utc": _plan_calibrated_at(plan),
                    "trigger": plan.trigger.value,
                    "coefficients": coefficients_jsonb(plan),
                    "window_start": plan.data_window_start_utc,
                    "window_end": plan.data_window_end_utc,
                    "audit_event_uuid": audit_event_uuid,
                    "notes": plan.audit_event.payload["notes"],
                },
            )
            if plan.becomes_head:
                await session.execute(
                    text(
                        "UPDATE slippage_calibration_versions "
                        "SET is_head = FALSE WHERE is_head = TRUE"
                    )
                )
                await session.execute(
                    text("UPDATE slippage_calibration_versions SET is_head = TRUE WHERE id = :id"),
                    {"id": plan.new_version_id},
                )


def _plan_calibrated_at(plan: CalibrationPlan) -> datetime:
    """Recover ``calibrated_at_utc`` from the plan's audit payload ``ts``.

    The planner echoes the caller's ``calibrated_at_utc`` into the payload's
    ``ts`` field (ISO 8601 with tz suffix); parsing it back keeps the row and
    the audit event byte-consistent without threading a second copy of the
    timestamp through this module.
    """
    ts: Any = plan.audit_event.payload["ts"]
    return datetime.fromisoformat(str(ts))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _amain(
    args: ParsedArgs,
    *,
    session_factory_override: async_sessionmaker[Any] | None = None,
) -> int:
    log_bound = log.bind(
        env=args.env,
        dry_run=args.dry_run,
        force_new_version=args.force_new_version,
    )
    log_bound.info("bootstrap_slippage_calibration_started")

    now_utc = datetime.now(tz=UTC)

    if args.dry_run:
        # No DB access on the dry-run path (mirrors bootstrap_live_account):
        # the printed plan assumes a fresh DB (version_no=1, no parent). The
        # wet run re-reads HEAD/version state from the DB before writing.
        plan = build_bootstrap_plan(
            new_version_id=mint_version_id(),
            calibrated_at_utc=now_utc,
            notes=args.notes,
        )
        log_bound.info(
            "bootstrap_slippage_calibration_dry_run_complete",
            version_no=plan.version_no,
            trigger=plan.trigger.value,
            fit_method=plan.fit_method.value,
            per_market_coefficients=plan.audit_event.payload["per_market_coefficients"],
            becomes_head=plan.becomes_head,
            fill_count=plan.fill_count,
            note=(
                "fresh-DB plan shown (version_no assumes no prior versions); "
                "no DB access, no writes. If a HEAD already exists the wet "
                "run is a no-op (or a successor with --force-new-version). "
                "Re-run with --no-dry-run --confirm to apply."
            ),
        )
        return EXIT_OK

    engine = None
    if session_factory_override is not None:
        session_factory: async_sessionmaker[Any] = session_factory_override
    else:
        database_url = os.environ.get(DATABASE_URL_ENV)
        if database_url is None or not database_url.strip():
            log_bound.error(
                "bootstrap_slippage_calibration_db_url_missing",
                note=f"set {DATABASE_URL_ENV} env var before invocation",
            )
            return EXIT_DB_INIT_FAILED
        try:
            engine = create_async_engine(database_url, future=True)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
        except Exception as exc:
            log_bound.error("bootstrap_slippage_calibration_db_init_failed", error=str(exc))
            return EXIT_DB_INIT_FAILED

    try:
        head = await _fetch_current_head(session_factory)
        if head is not None and not args.force_new_version:
            head_id, head_version_no = head
            log_bound.info(
                "bootstrap_slippage_calibration_head_exists",
                head_id=str(head_id),
                head_version_no=head_version_no,
                note=(
                    "a HEAD calibration row already exists; nothing to seed "
                    "(idempotent no-op). Pass --force-new-version to append "
                    "a successor zero-prior version."
                ),
            )
            return EXIT_OK

        account_id = await _resolve_active_account_id(session_factory)
        if account_id is None:
            log_bound.error(
                "bootstrap_slippage_calibration_no_account",
                note=(
                    "no active accounts row — the audit event needs an "
                    "account_id FK. Run bootstrap_live_account.py first "
                    "(deploy/crypto-vps-bringup.md Step 7)."
                ),
            )
            return EXIT_NO_ACCOUNT

        max_version_no = await _fetch_max_version_no(session_factory)
        parent_version_id = head[0] if head is not None else None
        if head is None and max_version_no is not None:
            log_bound.warning(
                "bootstrap_slippage_calibration_orphan_versions_no_head",
                max_version_no=max_version_no,
                note=(
                    "version rows exist but none is HEAD (unexpected state); "
                    "seeding a fresh zero-prior version at max+1 and "
                    "promoting it to HEAD."
                ),
            )

        plan = build_bootstrap_plan(
            new_version_id=mint_version_id(),
            calibrated_at_utc=now_utc,
            parent_version_id=parent_version_id,
            parent_version_no=max_version_no,
            notes=args.notes,
        )
        log_bound = log_bound.bind(
            new_version_id=str(plan.new_version_id),
            version_no=plan.version_no,
        )

        # --- step 1: audit-first (the writer owns its own txn + chain lock) ---
        from services.audit.writer import append_audit_event

        async with session_factory() as audit_session:
            record = await append_audit_event(
                audit_session,
                plan.audit_event.event_type,
                plan.audit_event.payload,
                account_id=account_id,
                env=args.env,
                phase_at_emit=1,
            )
        log_bound = log_bound.bind(audit_event_uuid=str(record.event_uuid))
        log_bound.info(
            "bootstrap_slippage_calibration_audit_emitted",
            sequence_no=record.sequence_no,
        )

        # --- steps 2+3: INSERT + HEAD swap, one atomic txn ---
        await _insert_version_and_swap_head(
            session_factory,
            plan=plan,
            audit_event_uuid=record.event_uuid,
        )
        log_bound.info(
            "bootstrap_slippage_calibration_completed",
            trigger=plan.trigger.value,
            fit_method=plan.fit_method.value,
            becomes_head=plan.becomes_head,
            parent_version_id=(str(parent_version_id) if parent_version_id else None),
            note=(
                "HEAD calibration row seeded; the strategy worker's "
                "no-slippage-head fail-closed gate is now satisfied."
            ),
        )
        return EXIT_OK
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log_bound.warning("bootstrap_slippage_calibration_db_dispose_error", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            return exc.code if isinstance(exc.code, int) else EXIT_BAD_ARGS
        log.error("bootstrap_slippage_calibration_bad_args", error=str(exc))
        return EXIT_BAD_ARGS
    try:
        return asyncio.run(_amain(args))
    except Exception as exc:  # pragma: no cover - terminal safety net
        log.error(
            "bootstrap_slippage_calibration_unexpected",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DATABASE_URL_ENV",
    "EXIT_BAD_ARGS",
    "EXIT_DB_INIT_FAILED",
    "EXIT_NO_ACCOUNT",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "ParsedArgs",
    "build_bootstrap_plan",
    "coefficients_jsonb",
    "main",
    "mint_version_id",
    "parse_args",
]
