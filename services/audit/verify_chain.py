"""Operator-facing CLI to verify ``audit_log`` hash-chain integrity.

Backend-spec §2.10.3 + implementation-guide §3 Week 4 Tue (verification gate
box 2). This module is a thin shell — argparse + ``asyncio.run`` +
``DATABASE_URL`` env var — over the canonical walker
:func:`services.audit.chain.verify_chain` (Day 8 PR #39). All hash-math
lives in ``services.audit.chain``; this file is responsible for argument
parsing, DB engine lifecycle, and the operator-readable stdout contract.

Same plan-then-apply shape as ``services/reconciliation/recon.py`` and
``services/risk/state_machine.py``: the policy (``verify_chain``) is pure
and testable; the I/O wrapper (this module) is the orchestration boundary.

Output contract (load-bearing for the operator-runbook gate at
``deploy/audit/README.md``)::

    On a clean walk of N rows:
        stdout: "CHAIN OK: N rows verified"
        exit:   0

    On a hash-chain break at row sequence_no=X (after K verified rows):
        stdout: "CHAIN BREAK at sequence_no=X (after K verified rows)"
        exit:   1

    On usage errors (DATABASE_URL unset, --env required, etc.):
        stderr: a single descriptive line pointing at deploy/audit/README.md
        exit:   2

The empty-table case prints ``"CHAIN OK: 0 rows verified"`` — vacuously
intact, which is a valid Phase-0 state before the first writer runs (no
audit events have been emitted yet on a fresh schema).

Connection lifecycle
--------------------

The CLI builds a one-shot SQLAlchemy async engine, opens a single session
for the walk, and disposes the engine on exit. We do NOT reuse
``services.api.db``'s shared pool because:

1. The CLI is invoked outside the api process (via ``docker compose exec``
   or directly on a developer laptop) — there is no shared pool to
   borrow.
2. A fresh single-connection engine forces an upper bound on the walk's
   resource footprint regardless of pool sizing.

The session uses the ``app_service`` role (per backend-spec §8.2 — same
role the api uses for non-audit reads). ``app_service`` is granted SELECT
on ``audit_log`` by ``alembic/versions/0006_roles.py`` and is the
expected role for read-only chain verification.

Forbidden whitelist
-------------------

This module lives under ``services/audit/**`` and therefore requires the
``risk-review-approved`` PR label per dev-guide §2.2 anti-pattern A02.
It is invoked against the real Ashburn DB at deploy/operator-runbook
time, so anti-pattern A27 (third-party platform integration smoke test)
binds — the smoke fixture is the operator-runbook checklist at
``deploy/audit/README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Final, Literal

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.audit.chain import verify_chain

log = structlog.get_logger()

#: Env var the CLI reads for the SQLAlchemy connection URL. Mirrors the
#: ``DATABASE_URL`` shape used by ``services/api/config.py`` (postgres
#: ``+asyncpg`` driver), but the var name here is bare ``DATABASE_URL``
#: so the runbook is conventional.
DATABASE_URL_ENV: Final[str] = "DATABASE_URL"

EXIT_OK: Final[int] = 0
EXIT_CHAIN_BREAK: Final[int] = 1
EXIT_USAGE: Final[int] = 2

#: Allowed values for ``--env``. Mirrors ``audit_log.env`` CHECK constraint
#: (``alembic/versions/0001_audit_log.py``) plus ``dev`` for laptop runs.
EnvName = Literal["dev", "paper", "live-small", "live-scale"]
_ALLOWED_ENVS: Final[tuple[str, ...]] = ("dev", "paper", "live-small", "live-scale")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="services.audit.verify_chain",
        description=(
            "Verify the audit_log hash chain end-to-end. "
            "Reads DATABASE_URL from the environment. See "
            "deploy/audit/README.md for the operator runbook."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Audit env tag — mirrors audit_log.env. Informational only; "
            "the exit code is the source of truth for the verification gate."
        ),
    )
    return parser


async def _verify(database_url: str) -> tuple[bool, int | None, int]:
    """Open one session, run the walker, dispose the engine. Single source
    of truth for the connection lifecycle so tests can monkeypatch this
    function directly without touching the asyncio loop or the engine."""
    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            return await verify_chain(session)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code; never sys.exits.

    Splitting this from the ``__main__`` guard keeps the function unit-
    testable end-to-end with monkeypatched env vars and a stubbed
    :func:`_verify` — see ``tests/unit/test_verify_chain_cli.py``.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    database_url = os.environ.get(DATABASE_URL_ENV)
    if not database_url:
        print(
            f"ERROR: {DATABASE_URL_ENV} is not set. "
            f"See deploy/audit/README.md Step 2 for the env var setup.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    log.info("verify_chain_started", env=args.env)
    ok, broken_sequence_no, rows_walked = asyncio.run(_verify(database_url))

    if ok:
        print(f"CHAIN OK: {rows_walked} rows verified")
        log.info(
            "verify_chain_passed",
            env=args.env,
            rows_walked=rows_walked,
        )
        return EXIT_OK

    print(f"CHAIN BREAK at sequence_no={broken_sequence_no} (after {rows_walked} verified rows)")
    log.error(
        "verify_chain_failed",
        env=args.env,
        broken_sequence_no=broken_sequence_no,
        rows_walked_before_break=rows_walked,
    )
    return EXIT_CHAIN_BREAK


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATABASE_URL_ENV",
    "EXIT_CHAIN_BREAK",
    "EXIT_OK",
    "EXIT_USAGE",
    "main",
]
