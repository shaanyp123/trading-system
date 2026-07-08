"""scripts/operator_tools/bootstrap_live_account.py — idempotent live-DB
account bootstrap.

Runs during the live-money cutover ceremony (per
``Docs/live-money-cutover-plan.md`` §10 steps 16-18) against a freshly
``alembic upgrade head``-migrated live Postgres, and — via
``--mint-from-defaults`` — to seed paper's baseline ``parameter_sets`` head row
(``Docs/parameter-sets-bootstrap-design.md`` §7 PR-A). Writes up to three rows:

  1. ``accounts`` — the live IBKR account row (default external_account_id
     ``U25655583`` per operator memory + cutover plan §10 step 1).
  2. ``risk_state`` — bootstrap row at state=``NORMAL``, severity=NULL,
     reason=``live_cutover``, is_current=TRUE. The audit_event_uuid slot
     is filled with a fresh UUID4 — matches the test-fixture pattern in
     ``tests/integration/test_kill_switch_end_to_end.py``. The
     ``risk_state`` row is what the api's ``fetch_current_risk_state``
     reads at startup to gate dispatching.
  3. ``parameter_sets`` (OPTIONAL) — supply the row one of two ways:
       * ``--parameter-set-json`` — INSERT a pre-extracted row with the
         supplied ``parameter_set_hash`` + ``parameters`` JSONB (the
         live-cutover "copy paper's head" path; operator extracts from
         paper's DB).
       * ``--mint-from-defaults`` — build the baseline row from the
         canonical defaults and MINT its ``parameter_set_hash`` via
         ``services/version/composite_hash.py`` (the paper-seed path; no
         JSON file needed). Stored ``parameters`` carries all canonical keys
         including ``STRATEGY_DECOMMISSIONED`` + ``EXIT_AUTO_APPROVE`` =
         False; the hash EXCLUDES those two flags (design §11 Q1-A) so a
         later decommission flip is a PK-stable in-place UPDATE.
     The two are mutually exclusive.

**Idempotency.** Each INSERT uses ``ON CONFLICT DO NOTHING`` against the
relevant unique constraint (accounts.external_account_id UNIQUE,
risk_state's ``account_id, is_current=TRUE`` partial unique index,
parameter_sets PRIMARY KEY = parameter_set_hash). Re-runs are no-ops.

**Why an operator script vs an alembic migration.** Account bootstrap
data is environment-specific (the live IBKR account ID is operator-side
state, not schema). Alembic migrations would couple schema to operator
identity. The script keeps the schema-vs-data split clean.

**Forbidden-paths check.** ``scripts/operator_tools/**`` is NOT on the
dev-guide §11 anti-pattern [A02] forbidden-modification whitelist nor
on the §2.3 hot-fix whitelist. Pure tooling — no ``services/risk/**``
modifications. The INSERT into ``risk_state`` crosses into risk-state
semantics, but only at bootstrap (no transition logic; canonical
``NORMAL`` initial value). Regular PR review applies.

**A-gates:**

* **A01 N/A** — no audit writes via this path. The risk_state row's
  ``audit_event_uuid`` is a synthesized UUID4 with NO matching ``audit_log``
  row. This is intentional for the cutover scenario (a fresh live DB has no
  upstream audit row yet) and is SAFE because ``risk_state.audit_event_uuid``
  is ``NOT NULL`` but carries **no FK** to ``audit_log`` (verified
  ``alembic/versions/0003_risk_tables.py``) — the synthesized UUID satisfies
  the column without a chain entry, and ``verify_chain`` (which walks
  ``audit_log`` only) is unaffected. Subsequent state transitions go through
  ``services.risk.dispatch`` / ``apply_state_transition`` which DO write audit
  rows. **Caveat (2026-05-30):** because this inserts a risk_state row WITHOUT
  an audit entry, running the FULL bootstrap against an already-running env is
  a state mutation that bypasses audit-first ordering (backend-spec §2.10.1) —
  another reason ``--seed-params-only`` is the correct mode for an existing
  account, and why the mismatch guard refuses the full path when an owner
  account already exists.
* **A05 N/A** — no Decimal handling in bootstrap.
* **A06 enforced** — every datetime tz-aware UTC.
* **A22 N/A** — testcontainers not needed; unit tests cover the
  argparse + idempotency + parameter_sets-loading surfaces with mocks.

Exit codes (load-bearing for the operator-runbook contract):

* ``0`` — success: all rows landed (or were already present; idempotent)
* ``1`` — invalid parameter-set JSON (missing ``parameter_set_hash`` or
  ``parameters`` keys; malformed JSON)
* ``4`` — existing-account mismatch: an active ``owner`` account already exists
  with a different ``external_account_id`` than ``--external-account-id`` (full
  bootstrap refuses; use ``--seed-params-only`` or fix the id). See the
  ``--seed-params-only`` mode + the 2026-05-30 dup-account note below.
* ``5`` — DB init failure (DATABASE_URL missing / wrong / Postgres
  unreachable)
* ``6`` — Invalid CLI args (caught early)
* ``99`` — Unexpected exception (logged with traceback)

Usage::

    # Stage 1 — minimum: account row + risk_state row.
    python -m scripts.operator_tools.bootstrap_live_account \\
        --external-account-id U25655583 \\
        --env live-small \\
        --allow-non-paper \\
        --no-dry-run --confirm

    # Stage 2 — also seed parameter_sets head row from paper extract:
    python -m scripts.operator_tools.bootstrap_live_account \\
        --external-account-id U25655583 \\
        --env live-small \\
        --parameter-set-json /tmp/paper_param_set_head.json \\
        --allow-non-paper \\
        --no-dry-run --confirm

    # Paper seed (CORRECT invocation) — seed ONLY the parameter_sets head row
    # from the canonical Amendment B defaults, skipping the account + risk_state inserts.
    # USE --seed-params-only on any already-bootstrapped env: the live paper
    # account's external_account_id is 'operator', NOT the IBKR number, so a
    # plain --mint-from-defaults run (without --seed-params-only) would create a
    # DUPLICATE account + a second is_current risk_state row (the 2026-05-30
    # footgun). Default --dry-run prints the minted hash; add --no-dry-run
    # --confirm to apply. --env paper needs no --allow-non-paper.
    python -m scripts.operator_tools.bootstrap_live_account \\
        --env paper \\
        --mint-from-defaults \\
        --seed-params-only \\
        --no-dry-run --confirm

The parameter-set JSON file shape::

    {
        "parameter_set_hash": "<64-char-hex>",
        "parameters": {"VOL_TARGET_PCT_ANNUAL": 0.15, ...}
    }

Operator extracts via paper VPS::

    docker compose exec postgres psql -U postgres -d trading -t -A -c "
      SELECT json_build_object(
        'parameter_set_hash', parameter_set_hash,
        'parameters', parameters
      ) FROM parameter_sets
      ORDER BY first_active_at DESC LIMIT 1;
    " > /tmp/paper_param_set_head.json
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
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.risk.crypto_parameters import default_crypto_parameters
from services.version.composite_hash import OPERATOR_ONLY_FLAG_KEYS, compute_parameter_set_hash

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit code contract (locked; documented in module docstring)
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_BAD_PARAM_SET_JSON: Final[int] = 1
EXIT_ACCOUNT_MISMATCH: Final[int] = 4
EXIT_DB_INIT_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Env values accepted on the CLI. Mirrors ``audit_log.env`` CHECK constraint.
_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

#: Operator's live IBKR account number per memory + cutover plan §10 step 1.
DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID: Final[str] = "U25655583"

#: DB connection env var. Mirrors other operator_tools scripts.
DATABASE_URL_ENV: Final[str] = "DATABASE_URL"


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests use this surface directly without going
    through argparse so they don't have to fight stderr capture.
    """

    external_account_id: str
    env: EnvName
    parameter_set_json: Path | None
    mint_from_defaults: bool
    seed_params_only: bool
    dry_run: bool
    confirm: bool
    allow_non_paper: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.bootstrap_live_account",
        description=(
            "Idempotent live-DB account bootstrap. Runs once during the "
            "cutover ceremony per Docs/live-money-cutover-plan.md §10 "
            "steps 16-18. Re-runs are safe (no-op)."
        ),
    )
    parser.add_argument(
        "--external-account-id",
        default=DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID,
        help=(
            f"IBKR account number (default: {DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID!r}). "
            "Persisted to accounts.external_account_id."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Audit env tag — mirrors audit_log.env CHECK. Live envs "
            "require --allow-non-paper safety gate."
        ),
    )
    parser.add_argument(
        "--parameter-set-json",
        type=Path,
        default=None,
        help=(
            "Optional path to a JSON file containing "
            '{"parameter_set_hash": "<64-hex>", "parameters": {...}}. '
            "When supplied, INSERTs a parameter_sets row idempotently. "
            "Operator pre-extracts from paper's DB."
        ),
    )
    parser.add_argument(
        "--mint-from-defaults",
        action="store_true",
        default=False,
        help=(
            "Build the baseline parameter_sets row from the canonical "
            "Amendment B crypto defaults (services/risk/crypto_parameters) and mint its "
            "parameter_set_hash via services/version/composite_hash.py, then "
            "INSERT idempotently. The stored 'parameters' carries all canonical "
            "keys including STRATEGY_DECOMMISSIONED + EXIT_AUTO_APPROVE = False; "
            "the hash EXCLUDES those two flags (design §11 Q1-A). Mutually "
            "exclusive with --parameter-set-json. Used to seed paper's head row."
        ),
    )
    parser.add_argument(
        "--seed-params-only",
        action="store_true",
        default=False,
        help=(
            "Seed ONLY the parameter_sets row; SKIP the accounts + risk_state "
            "inserts entirely. Use this when the account already exists and you "
            "just need to seed (or backfill) the parameter_sets head row — e.g. "
            "the paper seed (the live paper account's external_account_id is "
            "'operator', NOT the IBKR number, so the full bootstrap would create "
            "a duplicate account + a second is_current risk_state row). Requires "
            "a parameter-set source: --mint-from-defaults or --parameter-set-json."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "If set (default), log what WOULD be INSERTed without writing. "
            "Operator must pass --no-dry-run AND --confirm to actually "
            "write."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "Explicit confirmation for --no-dry-run. Two-flag gate per the "
            "replace_protective_stop.py convention. Fails closed otherwise."
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
    if parsed.mint_from_defaults and parsed.parameter_set_json is not None:
        raise ValueError(
            "--mint-from-defaults and --parameter-set-json are mutually exclusive: "
            "mint builds the row from canonical defaults, --parameter-set-json loads "
            "a pre-extracted row. Pick one."
        )
    if parsed.seed_params_only and not (
        parsed.mint_from_defaults or parsed.parameter_set_json is not None
    ):
        raise ValueError(
            "--seed-params-only requires a parameter-set source: pass "
            "--mint-from-defaults (build from canonical Amendment B defaults) or "
            "--parameter-set-json <file> (load a pre-extracted row). Without one "
            "there is nothing to seed."
        )
    return ParsedArgs(
        external_account_id=str(parsed.external_account_id),
        env=parsed.env,
        parameter_set_json=parsed.parameter_set_json,
        mint_from_defaults=bool(parsed.mint_from_defaults),
        seed_params_only=bool(parsed.seed_params_only),
        dry_run=bool(parsed.dry_run),
        confirm=bool(parsed.confirm),
        allow_non_paper=bool(parsed.allow_non_paper),
    )


@dataclass(frozen=True, slots=True)
class ParameterSetPayload:
    """Validated parameter_sets row to insert."""

    parameter_set_hash: str
    parameters: dict[str, Any]


def load_parameter_set_json(path: Path) -> ParameterSetPayload:
    """Read + validate the parameter-set JSON file.

    Raises ValueError on missing keys or malformed JSON; caller maps to
    EXIT_BAD_PARAM_SET_JSON.
    """
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"failed to read/parse {path}: {exc!s}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a JSON object")
    if "parameter_set_hash" not in raw:
        raise ValueError(f"{path}: missing 'parameter_set_hash' key")
    if "parameters" not in raw:
        raise ValueError(f"{path}: missing 'parameters' key")
    psh = raw["parameter_set_hash"]
    params = raw["parameters"]
    if not isinstance(psh, str):
        raise ValueError(f"{path}: 'parameter_set_hash' must be a string")
    if len(psh) != 64:
        raise ValueError(f"{path}: 'parameter_set_hash' must be 64 hex chars; got {len(psh)}")
    if not isinstance(params, dict):
        raise ValueError(f"{path}: 'parameters' must be a JSON object")
    return ParameterSetPayload(parameter_set_hash=psh, parameters=params)


def build_baseline_parameter_set_payload() -> ParameterSetPayload:
    """Mint the baseline ``parameter_sets`` row from the canonical defaults.

    Crypto-pivot C0-B4: the defaults are the Amendment B crypto profile
    (``services.risk.crypto_parameters``); the V1 defaults died with
    ``strategies/v1_trend_following``. Two load-bearing invariants
    (unchanged from the V1-era design §7 PR-A, §11 Q5-A):

    * **Stored ``parameters`` carries ALL canonical keys** — UPPER_CASE
      keys, decimals-as-strings, including both operator-only flags at
      their SAFE default ``False`` (locked L2). They render as the
      string ``"False"`` and round-trip to ``False`` via
      ``str(value).strip().lower() == "true"`` readers.
    * **The hash EXCLUDES the two flags** —
      :func:`services.version.composite_hash.compute_parameter_set_hash` hashes
      only the Parameter-Ranges-Table subset (design §11 Q1-A). This is what lets
      the §10.3 decommission ceremony flip ``STRATEGY_DECOMMISSIONED`` with an
      in-place ``parameters`` UPDATE that does NOT change the content-hash PK.
    """
    canonical = default_crypto_parameters()
    parameter_set_hash = compute_parameter_set_hash(canonical)
    return ParameterSetPayload(parameter_set_hash=parameter_set_hash, parameters=dict(canonical))


# ---------------------------------------------------------------------------
# I/O orchestration
# ---------------------------------------------------------------------------


async def _insert_account_idempotent(
    session_factory: async_sessionmaker[Any], *, external_account_id: str
) -> tuple[UUID, bool]:
    """INSERT the accounts row. Returns (account_id, created).

    ``created=True`` when the row was actually INSERTed; False when an
    existing row was found (idempotent re-run).
    """
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        async with session.begin():
            result = (
                await session.execute(
                    text(
                        "INSERT INTO accounts ("
                        "    external_account_id, account_type, base_currency, "
                        "    role, active_from"
                        ") VALUES ("
                        "    :ext, 'individual', 'USD', 'owner', :now"
                        ") "
                        "ON CONFLICT (external_account_id) "
                        "WHERE active_to IS NULL DO NOTHING "
                        "RETURNING id"
                    ),
                    {"ext": external_account_id, "now": now},
                )
            ).fetchone()
            if result is not None:
                return UUID(str(result.id)), True

        # Idempotent path — row already exists; look it up.
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM accounts "
                    "WHERE external_account_id = :ext AND active_to IS NULL "
                    "LIMIT 1"
                ),
                {"ext": external_account_id},
            )
        ).fetchone()
    if existing is None:
        # Shouldn't reach here — ON CONFLICT matched + RETURNING was empty,
        # so a row must exist. Defensive fail-loudly.
        raise RuntimeError(
            f"ON CONFLICT path took but no existing accounts row for "
            f"external_account_id={external_account_id!r}"
        )
    return UUID(str(existing.id)), False


async def _fetch_active_owner_accounts(
    session_factory: async_sessionmaker[Any],
) -> list[tuple[UUID, str]]:
    """Return all active (``active_to IS NULL``) ``owner`` accounts as
    ``(id, external_account_id)`` tuples.

    Used by the full-bootstrap mismatch guard: if an active owner account
    already exists with a DIFFERENT ``external_account_id`` than the one this
    run would insert, the account+risk_state bootstrap must NOT proceed (it
    would create a duplicate account + a second ``is_current`` risk_state row —
    the 2026-05-30 paper-seed footgun). The seed-params-only path skips this
    entirely.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, external_account_id FROM accounts "
                    "WHERE role = 'owner' AND active_to IS NULL "
                    "ORDER BY active_from ASC"
                )
            )
        ).fetchall()
    return [(UUID(str(r.id)), str(r.external_account_id)) for r in rows]


async def _insert_risk_state_idempotent(
    session_factory: async_sessionmaker[Any], *, account_id: UUID
) -> bool:
    """INSERT the risk_state bootstrap row. Returns ``created`` flag.

    The risk_state_current partial unique index (``account_id, is_current``
    WHERE ``is_current = TRUE``) gives us the idempotency primitive.
    """
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        async with session.begin():
            result = (
                await session.execute(
                    text(
                        "INSERT INTO risk_state ("
                        "    account_id, state, severity, reason, "
                        "    entered_at_utc, convalescent_session_count, "
                        "    vacation_active, audit_event_uuid, is_current"
                        ") VALUES ("
                        "    :acc, 'NORMAL', NULL, 'live_cutover', "
                        "    :now, 0, FALSE, :audit_uuid, TRUE"
                        ") "
                        # Column-based inference of the partial unique index
                        # `risk_state_current` (account_id, is_current) WHERE
                        # is_current = TRUE. CREATE UNIQUE INDEX makes an
                        # index, not a constraint — ON CONSTRAINT syntax
                        # won't match. The trailing WHERE clause picks the
                        # partial index unambiguously.
                        "ON CONFLICT (account_id, is_current) "
                        "WHERE is_current = TRUE "
                        "DO NOTHING "
                        "RETURNING id"
                    ),
                    {"acc": account_id, "now": now, "audit_uuid": uuid4()},
                )
            ).fetchone()
    return result is not None


async def _insert_parameter_set_idempotent(
    session_factory: async_sessionmaker[Any], *, payload: ParameterSetPayload
) -> bool:
    """INSERT the parameter_sets row. Returns ``created`` flag.

    parameter_sets is content-addressable (PK = hash); ON CONFLICT DO
    NOTHING is the idempotency primitive.
    """
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        async with session.begin():
            result = (
                await session.execute(
                    text(
                        "INSERT INTO parameter_sets ("
                        "    parameter_set_hash, parameters, first_active_at"
                        ") VALUES ("
                        "    :psh, CAST(:params AS JSONB), :now"
                        ") "
                        "ON CONFLICT (parameter_set_hash) DO NOTHING "
                        "RETURNING parameter_set_hash"
                    ),
                    {
                        "psh": payload.parameter_set_hash,
                        "params": json.dumps(payload.parameters),
                        "now": now,
                    },
                )
            ).fetchone()
    return result is not None


async def _amain(args: ParsedArgs) -> int:
    log_bound = log.bind(
        external_account_id=args.external_account_id,
        env=args.env,
        dry_run=args.dry_run,
        mint_from_defaults=args.mint_from_defaults,
        seed_params_only=args.seed_params_only,
        param_set_json=str(args.parameter_set_json) if args.parameter_set_json else None,
    )
    log_bound.info("bootstrap_live_account_started")

    payload: ParameterSetPayload | None = None
    if args.parameter_set_json is not None:
        try:
            payload = load_parameter_set_json(args.parameter_set_json)
        except ValueError as exc:
            log_bound.error("bootstrap_live_account_bad_param_set_json", error=str(exc))
            return EXIT_BAD_PARAM_SET_JSON
    elif args.mint_from_defaults:
        payload = build_baseline_parameter_set_payload()
        log_bound.info(
            "bootstrap_live_account_minted_baseline_parameter_set",
            parameter_set_hash=payload.parameter_set_hash,
            stored_key_count=len(payload.parameters),
            hashed_key_count=len(payload.parameters) - len(OPERATOR_ONLY_FLAG_KEYS),
            excluded_flags=sorted(OPERATOR_ONLY_FLAG_KEYS),
            note=(
                "stored parameters carry the 2 operator-only flags = False; "
                "the hash excludes them so a decommission flip is PK-stable."
            ),
        )

    if args.dry_run:
        log_bound.info(
            "bootstrap_live_account_dry_run_complete",
            would_insert_account=not args.seed_params_only,
            would_insert_risk_state=not args.seed_params_only,
            would_insert_parameter_set=payload is not None,
            note=(
                "seed-params-only: account + risk_state inserts SKIPPED; "
                if args.seed_params_only
                else ""
            )
            + "no DB writes; re-run with --no-dry-run --confirm to apply.",
        )
        return EXIT_OK

    database_url = os.environ.get(DATABASE_URL_ENV)
    if database_url is None or not database_url.strip():
        log_bound.error(
            "bootstrap_live_account_db_url_missing",
            note=f"set {DATABASE_URL_ENV} env var before invocation",
        )
        return EXIT_DB_INIT_FAILED

    engine = None
    try:
        engine = create_async_engine(database_url, future=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
    except Exception as exc:
        log_bound.error("bootstrap_live_account_db_init_failed", error=str(exc))
        return EXIT_DB_INIT_FAILED

    try:
        if args.seed_params_only:
            # Seed-params-only: SKIP the accounts + risk_state inserts entirely.
            # The account already exists; we only (re)seed the global,
            # account-independent parameter_sets head row. This is the correct
            # mode for the paper seed — the live paper account's
            # external_account_id is 'operator', not the IBKR number, so the
            # full bootstrap would create a duplicate account + a second
            # is_current risk_state row (the 2026-05-30 footgun).
            log_bound.info(
                "bootstrap_live_account_seed_params_only",
                note="account + risk_state inserts SKIPPED; seeding parameter_sets only.",
            )
        else:
            # Full bootstrap: guard against the existing-account mismatch that
            # caused the 2026-05-30 dup-account bug. If an active owner account
            # already exists with a DIFFERENT external_account_id than the one
            # we'd insert, refuse — inserting would create a duplicate account
            # + a second is_current risk_state row. The operator almost
            # certainly wants --seed-params-only (already-bootstrapped env) or
            # to pass --external-account-id matching the existing account.
            existing_owners = await _fetch_active_owner_accounts(session_factory)
            mismatched = [
                (aid, ext) for aid, ext in existing_owners if ext != args.external_account_id
            ]
            if mismatched:
                log_bound.error(
                    "bootstrap_live_account_existing_account_mismatch",
                    requested_external_account_id=args.external_account_id,
                    existing_external_account_ids=[ext for _, ext in existing_owners],
                    note=(
                        "An active owner account already exists with a different "
                        "external_account_id. Refusing to insert a duplicate account "
                        "+ second is_current risk_state row. If you only need to seed "
                        "the parameter_sets row, re-run with --seed-params-only. If "
                        "you intend to bootstrap THIS account, pass "
                        "--external-account-id matching the existing row."
                    ),
                )
                return EXIT_ACCOUNT_MISMATCH

            account_id, account_created = await _insert_account_idempotent(
                session_factory, external_account_id=args.external_account_id
            )
            log_bound = log_bound.bind(account_id=str(account_id))
            log_bound.info(
                "bootstrap_live_account_accounts_row",
                created=account_created,
                note=("inserted new row" if account_created else "idempotent no-op"),
            )

            risk_state_created = await _insert_risk_state_idempotent(
                session_factory, account_id=account_id
            )
            log_bound.info(
                "bootstrap_live_account_risk_state_row",
                created=risk_state_created,
                note=("inserted NORMAL row" if risk_state_created else "idempotent no-op"),
            )

        if payload is not None:
            param_created = await _insert_parameter_set_idempotent(session_factory, payload=payload)
            log_bound.info(
                "bootstrap_live_account_parameter_set_row",
                parameter_set_hash=payload.parameter_set_hash,
                created=param_created,
                note=("inserted new row" if param_created else "idempotent no-op"),
            )

        log_bound.info("bootstrap_live_account_completed")
        return EXIT_OK
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log_bound.warning("bootstrap_live_account_db_dispose_error", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            return exc.code if isinstance(exc.code, int) else EXIT_BAD_ARGS
        log.error("bootstrap_live_account_bad_args", error=str(exc))
        return EXIT_BAD_ARGS
    try:
        return asyncio.run(_amain(args))
    except Exception as exc:  # pragma: no cover - terminal safety net
        log.error(
            "bootstrap_live_account_unexpected",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID",
    "EXIT_ACCOUNT_MISMATCH",
    "EXIT_BAD_ARGS",
    "EXIT_BAD_PARAM_SET_JSON",
    "EXIT_DB_INIT_FAILED",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "ParameterSetPayload",
    "ParsedArgs",
    "build_baseline_parameter_set_payload",
    "load_parameter_set_json",
    "main",
    "parse_args",
]
