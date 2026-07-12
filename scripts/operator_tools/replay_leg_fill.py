"""scripts/operator_tools/replay_leg_fill.py — replay a leg fill through the
fill pipeline after a propagation failure.

**Why this exists (2026-07-12 incident):** the first live decision-driven
close leg filled at the venue and then ``process_fill_event`` raised
``EXIT_NO_PRIOR_TRADE`` (same-signal-id linkage mismatch, fixed alongside
this tool) — the decision died AFTER the venue trade, so ``orders`` stayed
'pending', no ``fills`` row landed, ``positions_current`` kept the stale
quantity, and the open ``trades`` row never closed. The 00:15 UTC recon
then (correctly) flagged the gap and auto-halted.

This tool re-feeds exactly one aggregated leg fill into
``services.risk.fill_processor.process_fill_event`` — the SAME pipeline
the worker uses — so the repair produces the full canonical record:
ORDER_FILLED → POSITION_UPDATED/CLOSED → BALANCE_SNAPSHOT_RECORDED →
TRADE_CLOSED audit events, the ``fills`` INSERT, the ``orders`` status
update, and the ``positions_current``/``trades`` mutations. No hand-written
SQL, no bypass of the audit chain.

**Operator inputs.** The fill economics come from the venue's own record —
read them off the ``order_placed`` audit payload's ``stages`` array (each
stage carries ``filled`` + ``fees_usd``; price from the Coinbase fills UI)
or the Coinbase web UI fills screen for the order:

* ``--client-order-id`` — the leg's deterministic client order id (the
  ``client_order_id`` on the stuck ``orders`` row / audit payload).
* ``--fill-quantity`` — total contracts filled (positive integer).
* ``--fill-price`` — average fill price (Decimal string).
* ``--commission-usd`` — total fees (Decimal string; ``0`` if unknown —
  understating fees only affects the trade PnL record, not positions).
* ``--filled-at-utc`` — ISO timestamp of the fill (defaults to now; pass
  the real time for a truthful audit record).

**Idempotency guard.** Refuses to run when the ``orders`` row is already
'filled' (the pipeline or fallback already recorded this fill) unless
``--force`` is passed. Dry-run by default: prints the located order row +
the payload it WOULD feed; ``--no-dry-run --confirm`` executes.

**Exit codes:** 0 success/dry-run; 2 validation; 3 order row not found /
already filled; 5 DB init failure.

On the VPS, run inside the api container with the in-container DATABASE_URL
(same wrapper ceremony as ``bootstrap_live_account`` — see
``deploy/crypto-vps-bringup.md`` Step 7 for the secrets-file pattern).

**Forbidden-paths check.** ``scripts/operator_tools/**`` is NOT on the
dev-guide §11 [A02] whitelist; this tool CALLS ``services/risk`` but
modifies nothing in it. A01 enforced — every write goes through the fill
pipeline's own ``append_audit_event`` calls.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.risk.fill_processor import (
    FillIngestPayload,
    FillProcessingError,
    process_fill_event,
)

log = structlog.get_logger()

DATABASE_URL_ENV: Final[str] = "DATABASE_URL"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replay_leg_fill",
        description="Replay one aggregated leg fill through the fill pipeline.",
    )
    parser.add_argument("--client-order-id", required=True)
    parser.add_argument("--fill-quantity", required=True, type=int)
    parser.add_argument("--fill-price", required=True)
    parser.add_argument("--commission-usd", default="0")
    parser.add_argument(
        "--filled-at-utc",
        default=None,
        help="ISO-8601 UTC timestamp of the fill; defaults to now.",
    )
    parser.add_argument("--env", default="paper", choices=["dev", "paper", "live"])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replay even when the orders row already shows status='filled'.",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Execute the replay (default: dry-run print only).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required together with --no-dry-run.",
    )
    return parser


async def _fetch_order_row(
    session_factory: async_sessionmaker[Any], client_order_id: str
) -> Any | None:
    async with session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT id, market, direction, quantity, status, created_at "
                    "FROM orders WHERE client_order_id = :cid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"cid": client_order_id},
            )
        ).fetchone()


async def _run(args: argparse.Namespace) -> int:
    database_url = os.environ.get(DATABASE_URL_ENV)
    if not database_url:
        print(f"ERROR: {DATABASE_URL_ENV} not set", file=sys.stderr)
        return 5

    try:
        fill_price = Decimal(args.fill_price)
        commission = Decimal(args.commission_usd)
    except InvalidOperation:
        print("ERROR: --fill-price/--commission-usd must be Decimal strings", file=sys.stderr)
        return 2
    if args.fill_quantity <= 0 or fill_price <= 0 or commission < 0:
        print("ERROR: quantity/price must be positive; commission >= 0", file=sys.stderr)
        return 2
    if args.filled_at_utc is not None:
        try:
            filled_at = datetime.fromisoformat(args.filled_at_utc)
        except ValueError:
            print("ERROR: --filled-at-utc is not valid ISO-8601", file=sys.stderr)
            return 2
        if filled_at.tzinfo is None:
            print("ERROR: --filled-at-utc must be tz-aware (A06)", file=sys.stderr)
            return 2
    else:
        filled_at = datetime.now(tz=UTC)

    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        order_row = await _fetch_order_row(session_factory, args.client_order_id)
        if order_row is None:
            print(f"ERROR: no orders row for client_order_id={args.client_order_id!r}")
            return 3
        print(
            f"orders row: id={order_row.id} market={order_row.market} "
            f"direction={order_row.direction} quantity={order_row.quantity} "
            f"status={order_row.status}"
        )
        if str(order_row.status) == "filled" and not args.force:
            print("ERROR: order already status='filled' — pass --force to replay anyway")
            return 3
        order_qty = int(order_row.quantity)
        if args.fill_quantity > order_qty and not args.force:
            print(
                f"ERROR: --fill-quantity {args.fill_quantity} exceeds the orders "
                f"row quantity {order_qty} — a typo here would mis-record the "
                "position. Pass --force only if the venue record truly says so.",
                file=sys.stderr,
            )
            return 2
        if args.fill_quantity != order_qty:
            print(
                f"WARNING: --fill-quantity {args.fill_quantity} != order quantity "
                f"{order_qty} (partial replay)"
            )

        payload = FillIngestPayload(
            broker_fill_id=f"{args.client_order_id}:agg",
            cumulative_filled_quantity=args.fill_quantity,
            fill_quantity=args.fill_quantity,
            fill_price=fill_price,
            commission_usd=commission,
            filled_at_utc=filled_at,
        )
        print(
            f"payload: qty={args.fill_quantity} price={fill_price} "
            f"fees={commission} filled_at={filled_at.isoformat()}"
        )

        if not args.no_dry_run:
            print("DRY RUN — nothing written. Re-run with --no-dry-run --confirm to execute.")
            return 0
        if not args.confirm:
            print("ERROR: --no-dry-run requires --confirm", file=sys.stderr)
            return 2

        try:
            result = await process_fill_event(
                session_factory=session_factory,
                client_order_id=args.client_order_id,
                payload=payload,
                env=args.env,
                phase_at_emit=1,
            )
        except FillProcessingError as exc:
            print(f"ERROR: fill pipeline refused the replay: {exc}", file=sys.stderr)
            return 3
        if result is None:
            print("ERROR: pipeline returned None (unknown order)", file=sys.stderr)
            return 3
        print(
            f"REPLAYED: order_status={result.new_order_status} "
            f"trade_id={result.trade_id} "
            f"audit_events={len(result.audit_event_uuids)}"
        )
        for u in result.audit_event_uuids:
            print(f"  audit_event_uuid={u}")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
