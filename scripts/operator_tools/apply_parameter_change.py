"""scripts/operator_tools/apply_parameter_change.py — operator-driven, audited
parameter-flag change (PR-D of ``Docs/parameter-sets-bootstrap-design.md`` §13).

Replaces the PR-B decommission runbook's **raw** ``UPDATE parameter_sets`` with
the proper event-sourced ceremony (Q4-B): an audit-first
``parameter_change_applied`` / ``parameter_change_reverted`` event + a
``parameters``-table history row + the ``parameter_sets`` head UPDATE — all so a
kill-switch flip carries an audit trail (exit-pipeline-design R6).

**Scope (§13.6).** This tool changes ONLY the two operator-only boolean flags
``STRATEGY_DECOMMISSIONED`` / ``EXIT_AUTO_APPROVE``. Those are **excluded from the
parameter_set_hash** (design Q1-A), so a flip is **hash-stable** — the head
``parameter_sets`` row is UPDATEd in place (Q2) and the new ``parameters`` row
carries ``parameter_set_hash == prev_parameter_set_hash`` (§13.3). Ranges
parameters (which DO move the hash → a new head row) are out of scope and go
through the agent's ``tighten_parameter`` lifecycle, never this tool.

**Ceremony (§13.4), audit-first per ``feedback_audit_first_ordering`` /
backend-spec §2.10.1:**

  1. ``append_audit_event(PARAMETER_CHANGE_{APPLIED,REVERTED}, …)`` — commits the
     audit row FIRST (the writer owns its own SERIALIZABLE + advisory-lock txn).
  2. One atomic txn for the state change: close the prior open ``parameters`` row
     (``valid_to = now``) → INSERT the new ``parameters`` row (FK
     ``audit_event_uuid`` from step 1; ``prev == hash``) → ``jsonb_set`` the head
     ``parameter_sets.parameters`` flag.

If step 2 fails after step 1, an audit row exists for a change that did not
apply — the correct audit-first behavior (the audit records intent; a failed
apply is diagnostically visible). Step 2's three statements are all-or-nothing.

**Forbidden-paths (§13.5).** This tool lives in ``scripts/operator_tools/**`` and
CALLS the existing ``services.audit.writer.append_audit_event`` + the EXISTING
``PARAMETER_CHANGE_APPLIED`` / ``_REVERTED`` event types (no ``event_types.py`` /
``alembic`` change). The diff is hot-fix scope — **NO ``risk-review-approved``
label is mechanically required** (same as ``recovery_agent.py``). It writes the
audit chain, so a **voluntary ``/ultrareview``** + the ``risk-review`` subagent
are recommended at review.

Exit codes (load-bearing for the runbook contract):

* ``0``  — success (change applied, no-op when already at value, or dry-run plan)
* ``1``  — failed parse-args validation (--no-dry-run without --confirm; live env
  without --allow-non-paper)
* ``2``  — argparse usage error (bad/missing flag — argparse's native code,
  passed through)
* ``3``  — no active ``parameter_sets`` head row (run the seed first:
  ``bootstrap_live_account.py --mint-from-defaults``)
* ``4``  — no active ``accounts`` row (run ``bootstrap_live_account.py`` first)
* ``5``  — DB init failure (DATABASE_URL missing / unreachable)
* ``99`` — unexpected exception

Usage::

    # Dry-run (default) — validates + prints the planned ceremony, no DB writes:
    python -m scripts.operator_tools.apply_parameter_change \\
        --env paper --parameter STRATEGY_DECOMMISSIONED --value true \\
        --reason "decommission smoke 2026-05-30"

    # Wet — flip the kill-switch ON (two-flag gate):
    python -m scripts.operator_tools.apply_parameter_change \\
        --env paper --parameter STRATEGY_DECOMMISSIONED --value true \\
        --reason "decommission smoke" --no-dry-run --confirm

    # Revert (OFF) — emits parameter_change_reverted:
    python -m scripts.operator_tools.apply_parameter_change \\
        --env paper --parameter STRATEGY_DECOMMISSIONED --value false \\
        --reason "revert smoke" --no-dry-run --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit-code contract
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_BAD_INPUT: Final[int] = 1
# NB: argparse's native usage-error exit is 2 (passed through by main); our codes
# avoid 2 so a runbook can distinguish an argparse error from a domain guard.
EXIT_NO_HEAD_ROW: Final[int] = 3
EXIT_NO_ACCOUNT: Final[int] = 4
EXIT_DB_INIT_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# Scope: operator-only boolean flags ONLY (hash-stable per Q1-A / §13.3)
# ---------------------------------------------------------------------------

#: The ONLY parameters this tool may change. Both are excluded from the
#: parameter_set_hash (design Q1-A), so a flip is hash-stable. Ranges params are
#: deliberately NOT here — they move the hash and go through the agent path.
_ALLOWED_PARAMETERS: Final[tuple[str, ...]] = (
    "STRATEGY_DECOMMISSIONED",
    "EXIT_AUTO_APPROVE",
)

_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

DATABASE_URL_ENV: Final[str] = "DATABASE_URL"

#: Canonical stored shape for the flags — matches V1_DEFAULTS / the PR-A seed +
#: the trigger_v1_cycle / lean ``_coerce_bool_param`` readers.
_TRUE_STR: Final[str] = "True"
_FALSE_STR: Final[str] = "False"


def _value_to_canonical_str(value: bool) -> str:
    return _TRUE_STR if value else _FALSE_STR


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    env: EnvName
    parameter: str
    value: bool
    reason: str | None
    dry_run: bool
    confirm: bool
    allow_non_paper: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.apply_parameter_change",
        description=(
            "Operator-driven audited change to an operator-only boolean flag "
            "(STRATEGY_DECOMMISSIONED / EXIT_AUTO_APPROVE). Audit-first ceremony "
            "per parameter-sets-bootstrap-design §13."
        ),
    )
    parser.add_argument("--env", required=True, choices=_ALLOWED_ENVS)
    parser.add_argument(
        "--parameter",
        required=True,
        choices=_ALLOWED_PARAMETERS,
        help="The operator-only flag to change (hash-stable; ranges params are out of scope).",
    )
    parser.add_argument(
        "--value",
        required=True,
        choices=("true", "false"),
        help="New value (true/false). Setting True emits parameter_change_applied; "
        "False emits parameter_change_reverted.",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="Operator change_reason recorded on the audit event + parameters row.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If set (default), validate + print the planned ceremony without DB writes. "
        "Pass --no-dry-run AND --confirm to apply.",
    )
    parser.add_argument("--confirm", action="store_true", default=False)
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        help="Required for --env live-small / live-scale.",
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
        parameter=str(parsed.parameter),
        value=(parsed.value == "true"),
        reason=parsed.reason,
        dry_run=bool(parsed.dry_run),
        confirm=bool(parsed.confirm),
        allow_non_paper=bool(parsed.allow_non_paper),
    )


# ---------------------------------------------------------------------------
# Pure payload builder (testable without a DB)
# ---------------------------------------------------------------------------


def _build_parameter_change_payload(
    *,
    parameter_name: str,
    old_value_str: str,
    new_value_str: str,
    parameter_set_hash: str,
    changed_by: str,
    change_reason: str | None,
) -> dict[str, Any]:
    """Compose the audit_log payload (JCS-safe types only).

    ``prev_parameter_set_hash == parameter_set_hash`` because the flag is
    excluded from the hash (Q1-A / §13.3) — the flip is hash-stable.
    """
    return {
        "parameter_name": parameter_name,
        "old_value": old_value_str,
        "new_value": new_value_str,
        "parameter_set_hash": parameter_set_hash,
        "prev_parameter_set_hash": parameter_set_hash,
        "changed_by": changed_by,
        "change_reason": change_reason,
    }


def _changed_by_for(new_value: bool) -> str:
    """Setting a flag to its non-default (True) is an operator APPLY; returning it
    to the default (False) is a REVERT (§13.4)."""
    return "operator" if new_value else "revert"


# ---------------------------------------------------------------------------
# DB reads + the atomic state change
# ---------------------------------------------------------------------------


async def _resolve_active_account_id(session_factory: async_sessionmaker[Any]) -> UUID | None:
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


async def _fetch_active_head(
    session_factory: async_sessionmaker[Any],
) -> tuple[str, dict[str, Any]] | None:
    """Return (parameter_set_hash, parameters) for the active head row, or None."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT parameter_set_hash, parameters FROM parameter_sets "
                    "WHERE last_active_at IS NULL ORDER BY first_active_at DESC LIMIT 1"
                )
            )
        ).fetchone()
    if row is None:
        return None
    return str(row.parameter_set_hash), dict(row.parameters)


async def _apply_state_change(
    session_factory: async_sessionmaker[Any],
    *,
    parameter_name: str,
    value_str: str,
    parameter_set_hash: str,
    audit_event_uuid: UUID,
    changed_by: str,
    change_reason: str | None,
    now: datetime,
) -> None:
    """The §13.4 step-2 atomic state change: close prior parameters row → INSERT
    new (prev == hash) → jsonb_set the parameter_sets head. All-or-nothing."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE parameters SET valid_to = :now "
                    "WHERE parameter_name = :pname AND valid_to IS NULL"
                ),
                {"now": now, "pname": parameter_name},
            )
            await session.execute(
                text(
                    "INSERT INTO parameters ("
                    "    parameter_name, parameter_value, valid_from, valid_to, "
                    "    changed_by, change_reason, parameter_set_hash, "
                    "    prev_parameter_set_hash, audit_event_uuid"
                    ") VALUES ("
                    "    :pname, to_jsonb(CAST(:pval AS text)), :now, NULL, "
                    "    :changed_by, :reason, :hash, :prev_hash, :audit_uuid"
                    ")"
                ),
                {
                    "pname": parameter_name,
                    "pval": value_str,
                    "now": now,
                    "changed_by": changed_by,
                    "reason": change_reason,
                    "hash": parameter_set_hash,
                    # prev == hash: the flip is hash-stable (Q1-A / §13.3).
                    "prev_hash": parameter_set_hash,
                    "audit_uuid": audit_event_uuid,
                },
            )
            await session.execute(
                text(
                    "UPDATE parameter_sets "
                    "SET parameters = jsonb_set("
                    "    parameters, ARRAY[:pname], to_jsonb(CAST(:pval AS text))"
                    ") WHERE last_active_at IS NULL"
                ),
                {"pname": parameter_name, "pval": value_str},
            )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _amain(
    args: ParsedArgs,
    *,
    session_factory_override: async_sessionmaker[Any] | None = None,
) -> int:
    new_value_str = _value_to_canonical_str(args.value)
    changed_by = _changed_by_for(args.value)
    log_bound = log.bind(
        env=args.env,
        parameter=args.parameter,
        new_value=new_value_str,
        changed_by=changed_by,
        dry_run=args.dry_run,
    )
    log_bound.info("apply_parameter_change_started")

    if args.dry_run:
        log_bound.info(
            "apply_parameter_change_dry_run_complete",
            would_emit_event=(
                "parameter_change_applied" if args.value else "parameter_change_reverted"
            ),
            note=(
                "no DB writes; the wet run resolves the active account + head row "
                "(erroring if absent), then runs the audit-first ceremony. Re-run "
                "with --no-dry-run --confirm to apply."
            ),
        )
        return EXIT_OK

    engine = None
    if session_factory_override is not None:
        session_factory: async_sessionmaker[Any] = session_factory_override
    else:
        database_url = os.environ.get(DATABASE_URL_ENV)
        if database_url is None or not database_url.strip():
            log_bound.error("apply_parameter_change_db_url_missing", note=f"set {DATABASE_URL_ENV}")
            return EXIT_DB_INIT_FAILED
        try:
            engine = create_async_engine(database_url, future=True)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
        except Exception as exc:
            log_bound.error("apply_parameter_change_db_init_failed", error=str(exc))
            return EXIT_DB_INIT_FAILED

    try:
        head = await _fetch_active_head(session_factory)
        if head is None:
            log_bound.error(
                "apply_parameter_change_no_head_row",
                note="parameter_sets has no active head row — run "
                "bootstrap_live_account.py --mint-from-defaults first.",
            )
            return EXIT_NO_HEAD_ROW
        parameter_set_hash, head_params = head
        old_value_str = str(head_params.get(args.parameter, _FALSE_STR))

        if old_value_str == new_value_str:
            log_bound.info(
                "apply_parameter_change_noop_already_at_value",
                current_value=old_value_str,
                note="parameter already at the requested value; no audit row, no write.",
            )
            return EXIT_OK

        account_id = await _resolve_active_account_id(session_factory)
        if account_id is None:
            log_bound.error(
                "apply_parameter_change_no_account",
                note="no active accounts row — run bootstrap_live_account.py first.",
            )
            return EXIT_NO_ACCOUNT

        # --- audit-first (step 1): the writer owns its own txn + chain lock ---
        from services.audit.event_types import AuditEventType
        from services.audit.writer import append_audit_event

        event_type = (
            AuditEventType.PARAMETER_CHANGE_APPLIED
            if args.value
            else AuditEventType.PARAMETER_CHANGE_REVERTED
        )
        payload = _build_parameter_change_payload(
            parameter_name=args.parameter,
            old_value_str=old_value_str,
            new_value_str=new_value_str,
            parameter_set_hash=parameter_set_hash,
            changed_by=changed_by,
            change_reason=args.reason,
        )
        async with session_factory() as audit_session:
            record = await append_audit_event(
                audit_session,
                event_type,
                payload,
                account_id=account_id,
                env=args.env,
                phase_at_emit=1,
            )
        log_bound = log_bound.bind(audit_event_uuid=str(record.event_uuid))
        log_bound.info("apply_parameter_change_audit_emitted", sequence_no=record.sequence_no)

        # --- state change (step 2): one atomic txn, AFTER the audit commit ---
        now = datetime.now(tz=UTC)
        await _apply_state_change(
            session_factory,
            parameter_name=args.parameter,
            value_str=new_value_str,
            parameter_set_hash=parameter_set_hash,
            audit_event_uuid=record.event_uuid,
            changed_by=changed_by,
            change_reason=args.reason,
            now=now,
        )
        log_bound.info(
            "apply_parameter_change_completed",
            old_value=old_value_str,
            parameter_set_hash=parameter_set_hash,
            note="hash unchanged (flag excluded from hash, Q1-A); head UPDATEd in place.",
        )
        return EXIT_OK
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log_bound.warning("apply_parameter_change_db_dispose_error", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            return exc.code if isinstance(exc.code, int) else EXIT_BAD_ARGS
        log.error("apply_parameter_change_bad_args", error=str(exc))
        return EXIT_BAD_INPUT
    try:
        return asyncio.run(_amain(args))
    except Exception as exc:  # pragma: no cover - terminal safety net
        log.error(
            "apply_parameter_change_unexpected",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DATABASE_URL_ENV",
    "EXIT_BAD_ARGS",
    "EXIT_BAD_INPUT",
    "EXIT_DB_INIT_FAILED",
    "EXIT_NO_ACCOUNT",
    "EXIT_NO_HEAD_ROW",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "ParsedArgs",
    "_apply_state_change",
    "_build_parameter_change_payload",
    "_changed_by_for",
    "main",
    "parse_args",
]
