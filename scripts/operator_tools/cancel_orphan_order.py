"""scripts/operator_tools/cancel_orphan_order.py — cancel a stuck/orphan
order via the sanctioned audit-first terminal-status path.

Some order-placement failures leave an ``orders`` row stuck at
``status='pending'`` with ``broker_order_id IS NULL`` — the order never
reached IBKR. The canonical example is the 2026-06-04 ``/MYM`` order:
``resolve_contract`` sent ``exchange="CME"`` for a CBOT contract → IBKR
**Error 200** → ``order_placement_broker_unavailable``, no fill, the
pre-inserted row left at ``pending`` (see PR #327 for the exchange fix).
The order-placement worker's ``fetch_approved_signals`` then excludes the
parent signal forever (its ``o.id IS NULL`` filter is false once any order
row exists), so the order is **inert** but dangles in a non-terminal state.

This tool transitions such an order to a terminal state cleanly. It:

* does **NOT** touch IBKR;
* **refuses** to act on an order that may be live at the broker
  (``broker_order_id IS NOT NULL``) — cancelling a live order requires an
  actual IBKR cancel, not a DB-only flip, so that path is out of scope here;
* reuses the tested, audit-first
  :func:`services.risk.order_terminal_status_processor.process_terminal_status_event`
  (``ORDER_CANCELLED`` / ``ORDER_REJECTED`` audit row committed BEFORE the
  ``orders`` UPDATE per backend-spec §2.10.1) so the audit hash-chain stays
  intact — this tool writes no SQL UPDATE and no audit row itself;
* is **dry-run by default**; pass ``--execute`` to mutate.

A02: ``scripts/operator_tools/**`` is the §2.3 hot-fix lane (no
``risk-review-approved`` label). This tool only *calls* the forbidden-path
processor; it does not modify it.

Run inside the api container (where secrets + DB access live), e.g.::

    docker exec -i trading-api-1 /opt/venv/bin/python -m \
      scripts.operator_tools.cancel_orphan_order \
      --client-order-id 2df34f8f-65e4069f-019e948b-0 \
      --reason "orphaned /MYM entry: placement failed IBKR Error 200 (CME-for-CBOT, PR #327); breakout fizzled, operator chose not to chase (2026-06-05); never reached the broker" \
      --execute
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog

log = structlog.get_logger()

EnvName = Literal["paper", "live-small", "live-scale"]

#: orders.status values that are already terminal — re-cancelling is a no-op.
TERMINAL_STATUSES: frozenset[str] = frozenset({"filled", "cancelled", "rejected"})

DATABASE_URL_ENV = "API_DATABASE_URL"

# Exit codes.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ORDER_NOT_FOUND = 2
EXIT_LIVE_AT_BROKER = 3
EXIT_DB_INIT_FAILED = 4


def classify_order_for_cancellation(*, status: str, broker_order_id: int | None) -> tuple[str, int]:
    """Pure-policy decision for whether a tool-driven cancel is safe.

    Returns ``(decision, exit_code)`` where ``decision`` is one of:

    * ``"already_terminal"`` — ``status`` is filled/cancelled/rejected; the
      processor would treat it as an idempotent no-op. Exit 0.
    * ``"live_at_broker"`` — ``broker_order_id`` is set, so the order may be
      live at IBKR. Refuse: a DB-only flip would desync from the broker;
      use an actual IBKR cancel instead. Non-zero exit.
    * ``"proceed"`` — non-terminal status AND no broker order id → the order
      never reached the broker; safe to cancel via the audit-first path.
    """
    if status in TERMINAL_STATUSES:
        return "already_terminal", EXIT_OK
    if broker_order_id is not None:
        return "live_at_broker", EXIT_LIVE_AT_BROKER
    return "proceed", EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cancel_orphan_order",
        description=(
            "Cancel a stuck/orphan order (DB row never placed at IBKR) via the "
            "sanctioned audit-first terminal-status processor. Dry-run by default."
        ),
    )
    parser.add_argument(
        "--client-order-id",
        required=True,
        help="The orders.client_order_id of the stuck order (33-char CID).",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help=(
            "Human-readable rejection_reason recorded on the order row + in the "
            "audit payload. Required — explains why the order is being cancelled."
        ),
    )
    parser.add_argument(
        "--status-kind",
        choices=("cancelled", "rejected"),
        default="cancelled",
        help="Terminal status to apply (default: cancelled).",
    )
    parser.add_argument(
        "--env",
        choices=("paper", "live-small", "live-scale"),
        default="paper",
        help="Audit env tag (default: paper).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the cancellation. Omit for a dry-run preview (default).",
    )
    return parser


def _resolve_database_url() -> str:
    """Resolve the SQLAlchemy async DB URL.

    Prefers ``API_DATABASE_URL`` from the environment; otherwise builds it
    from the in-container sops bundle via the api entrypoint helpers (the
    decrypted secrets are mounted at ``API_SECRETS_PATH``). The password is
    never logged.
    """
    env_url = os.environ.get(DATABASE_URL_ENV)
    if env_url:
        return env_url
    from services.api.entrypoint import (
        DEFAULT_SECRETS_PATH,
        _build_database_url,
        _load_secrets,
    )

    secrets_path = Path(os.environ.get("API_SECRETS_PATH", str(DEFAULT_SECRETS_PATH)))
    secrets = _load_secrets(secrets_path)
    pg_password = (secrets.get("postgres") or {}).get("app_service_password")
    if not isinstance(pg_password, str) or not pg_password:
        raise RuntimeError(
            f"postgres.app_service_password missing in {secrets_path}; "
            f"set {DATABASE_URL_ENV} or fix the sops bundle"
        )
    return _build_database_url(pg_password)


async def _build_session_factory(database_url: str) -> tuple[Any, Any]:
    """One-shot async engine + sessionmaker (mirrors recovery_agent.py)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def _amain(args: argparse.Namespace, *, session_factory_override: Any = None) -> int:
    from sqlalchemy import text

    engine: Any = None
    session_factory: Any = session_factory_override
    if session_factory is None:
        try:
            database_url = _resolve_database_url()
            engine, session_factory = await _build_session_factory(database_url)
        except Exception as exc:
            log.error("cancel_orphan_order_db_init_failed", error=str(exc))
            return EXIT_DB_INIT_FAILED

    try:
        # Step 1: look up the order row.
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, signal_id, market, status, broker_order_id "
                        "FROM orders WHERE client_order_id = :coid LIMIT 1"
                    ),
                    {"coid": args.client_order_id},
                )
            ).fetchone()

        if row is None:
            log.error("cancel_orphan_order_not_found", client_order_id=args.client_order_id)
            return EXIT_ORDER_NOT_FOUND

        broker_order_id = int(row.broker_order_id) if row.broker_order_id is not None else None
        decision, decision_exit_code = classify_order_for_cancellation(
            status=str(row.status), broker_order_id=broker_order_id
        )

        log.info(
            "cancel_orphan_order_target",
            client_order_id=args.client_order_id,
            order_id=str(row.id),
            signal_id=str(row.signal_id) if row.signal_id is not None else None,
            market=str(row.market),
            current_status=str(row.status),
            broker_order_id=broker_order_id,
            decision=decision,
            status_kind=args.status_kind,
            execute=bool(args.execute),
        )

        if decision == "already_terminal":
            log.info(
                "cancel_orphan_order_noop_already_terminal",
                current_status=str(row.status),
            )
            return decision_exit_code
        if decision == "live_at_broker":
            log.error(
                "cancel_orphan_order_refused_live_at_broker",
                broker_order_id=broker_order_id,
                detail=(
                    "order has a broker_order_id and may be live at IBKR; "
                    "DB-only cancel would desync — cancel it at the broker instead"
                ),
            )
            return decision_exit_code

        # decision == "proceed"
        if not args.execute:
            log.info(
                "cancel_orphan_order_dry_run",
                detail="dry-run: no changes written; re-run with --execute to apply",
                would_set_status=args.status_kind,
                would_set_rejection_reason=args.reason,
            )
            return EXIT_OK

        # Step 2: audit-first terminal-status transition (reused processor).
        from services.risk.order_terminal_status_processor import (
            TerminalStatusPayload,
            process_terminal_status_event,
        )

        result = await process_terminal_status_event(
            session_factory=session_factory,
            client_order_id=args.client_order_id,
            payload=TerminalStatusPayload(
                broker_order_id=0,  # never reached the broker (broker_order_id IS NULL)
                status_kind=args.status_kind,
                rejection_reason=args.reason,
                observed_at_utc=datetime.now(tz=UTC),
            ),
            env=args.env,
            phase_at_emit=1,
        )
        if result is None:
            # Raced to terminal between SELECT and processor; idempotent no-op.
            log.info("cancel_orphan_order_noop_raced_terminal")
            return EXIT_OK
        log.info(
            "cancel_orphan_order_done",
            order_id=str(result.order_id),
            new_status=result.new_status,
            audit_event_uuid=str(result.audit_event_uuid),
            audit_sequence_no=result.audit_sequence_no,
        )
        return EXIT_OK
    except Exception as exc:
        log.error(
            "cancel_orphan_order_failed",
            error=str(exc),
            error_class=type(exc).__name__,
        )
        return EXIT_ERROR
    finally:
        if engine is not None:
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _build_parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
