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

**Missing-orders-row repair mode (2026-07-16 incident).** The night-three
variant crashed AFTER ``insert_order_row``; the 2026-07-16 variant crashed
BEFORE it (venue filled the close, but the leg died between
``execute_target_delta`` and the orders INSERT), so the ``orders`` row the
replay needs does not exist — only the leg's ``signals`` row landed (it is
written before the venue is touched, so a venue fill proves it exists).
``--create-order-row --signal-id <uuid> --order-direction buy|sell`` first
records the missing orders row through the SAME production path the worker
uses (``StrategyWorkerStore.insert_order_row``: audit-first ORDER_PLACED
with an explicit ``repair`` marker, then the INSERT), then replays the fill
through the pipeline as usual. Validations (each overridable ONLY with
``--force``): the signal must exist, be ``signal_type='exit'``, and match
an open ``positions_current`` row whose sign the order direction actually
closes (short → buy, long → sell) with ``|quantity| == --fill-quantity``.

**Exit codes:** 0 success/dry-run; 2 validation; 3 order row not found /
already filled; 4 signal row not found / repair validation failed;
5 DB init failure.

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
    parser.add_argument(
        "--create-order-row",
        action="store_true",
        help=(
            "When no orders row exists for --client-order-id, create it from "
            "the leg's signals row (via StrategyWorkerStore.insert_order_row) "
            "before replaying. Requires --signal-id and --order-direction."
        ),
    )
    parser.add_argument(
        "--signal-id",
        default=None,
        help="signals.id (UUID) of the leg whose orders row is missing.",
    )
    parser.add_argument(
        "--order-direction",
        default=None,
        choices=["buy", "sell"],
        help="Order side for the created row (closing a short = buy).",
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


async def _fetch_signal_row(session_factory: async_sessionmaker[Any], signal_id: str) -> Any | None:
    async with session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT id, account_id, market, contract_id, direction, "
                    "       signal_type, target_contracts, strategy_hash, "
                    "       parameter_set_hash, status, emitted_at_utc "
                    "FROM signals WHERE id = :sid"
                ),
                {"sid": signal_id},
            )
        ).fetchone()


async def _fetch_position_row(
    session_factory: async_sessionmaker[Any], *, account_id: Any, market: str
) -> Any | None:
    async with session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT quantity FROM positions_current "
                    "WHERE account_id = :acct AND market = :market "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"acct": account_id, "market": market},
            )
        ).fetchone()


async def _create_missing_order_row(
    *,
    session_factory: async_sessionmaker[Any],
    args: argparse.Namespace,
    execute: bool,
) -> int:
    """Validate (always) + create (only when ``execute``) the missing
    orders row from the leg's signal.

    Returns 0 on success (validations passed; row created when
    ``execute=True`` — caller proceeds to replay), or the exit code to
    bail with. Validations are identical in dry-run and execute so the
    dry-run output is a faithful preview.
    """
    signal = await _fetch_signal_row(session_factory, args.signal_id)
    if signal is None:
        print(f"ERROR: no signals row for --signal-id={args.signal_id!r}", file=sys.stderr)
        return 4
    if str(signal.signal_type) != "exit" and not args.force:
        print(
            f"ERROR: signal {args.signal_id} has signal_type="
            f"{signal.signal_type!r}, expected 'exit' (the stranded-close "
            "repair). Pass --force only if you are certain.",
            file=sys.stderr,
        )
        return 4

    position = await _fetch_position_row(
        session_factory, account_id=signal.account_id, market=str(signal.market)
    )
    pos_qty = int(position.quantity) if position is not None else 0
    closes_short = args.order_direction == "buy" and pos_qty < 0
    closes_long = args.order_direction == "sell" and pos_qty > 0
    if not (closes_short or closes_long) and not args.force:
        print(
            f"ERROR: positions_current for {signal.market} is {pos_qty}; an "
            f"--order-direction {args.order_direction} order does not close "
            "it. Pass --force only if the book state is already known-wrong.",
            file=sys.stderr,
        )
        return 4
    if abs(pos_qty) != args.fill_quantity and not args.force:
        print(
            f"ERROR: --fill-quantity {args.fill_quantity} != |positions_current| "
            f"{abs(pos_qty)} — a full-close replay must take the position to "
            "exactly zero. Pass --force only if the venue record truly says so.",
            file=sys.stderr,
        )
        return 4

    print(
        f"missing orders row would be created from signal {signal.id}: "
        f"market={signal.market} direction={args.order_direction} "
        f"quantity={args.fill_quantity} order_type=limit_marketable "
        f"(positions_current={pos_qty})"
    )
    if not execute:
        return 0

    # Local import: the store pulls the worker module (pandas etc.); only
    # this repair mode needs it, and the api image ships the full tree.
    from services.signal.strategy_worker import StrategyWorkerStore

    store = StrategyWorkerStore(session_factory=session_factory, env=args.env)
    order_id = await store.insert_order_row(
        signal.account_id,
        signal_id=signal.id,
        client_order_id=args.client_order_id,
        broker_order_id=None,
        market=str(signal.market),
        contract_id=signal.contract_id,
        direction=args.order_direction,
        order_type="limit_marketable",
        quantity=args.fill_quantity,
        limit_price=None,
        stop_price=None,
        now_utc=datetime.now(tz=UTC),
        strategy_hash=str(signal.strategy_hash),
        parameter_set_hash=str(signal.parameter_set_hash),
        audit_payload={
            "repair": "replay_leg_fill --create-order-row",
            "note": (
                "orders row recorded post-hoc: the leg crashed after the "
                "venue fill but before insert_order_row (2026-07-16 "
                "stranded-close variant); fill economics follow in the "
                "replayed ORDER_FILLED event"
            ),
        },
    )
    print(f"orders row created: id={order_id} (audit-first ORDER_PLACED with repair marker)")
    return 0


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
    if args.no_dry_run and not args.confirm:
        print("ERROR: --no-dry-run requires --confirm", file=sys.stderr)
        return 2

    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        order_row = await _fetch_order_row(session_factory, args.client_order_id)
        if order_row is None and args.create_order_row:
            if not args.signal_id or not args.order_direction:
                print(
                    "ERROR: --create-order-row requires --signal-id and --order-direction",
                    file=sys.stderr,
                )
                return 2
            rc = await _create_missing_order_row(
                session_factory=session_factory, args=args, execute=args.no_dry_run
            )
            if rc != 0:
                return rc
            if args.no_dry_run:
                order_row = await _fetch_order_row(session_factory, args.client_order_id)
                if order_row is None:
                    print(
                        "ERROR: orders row still missing after create — investigate",
                        file=sys.stderr,
                    )
                    return 4
        if order_row is None and not args.no_dry_run and args.create_order_row:
            # Dry-run preview of the create path: no row to print yet; the
            # payload block below still previews the replay economics.
            pass
        elif order_row is None:
            print(
                f"ERROR: no orders row for client_order_id={args.client_order_id!r} "
                "(if the leg crashed before insert_order_row, re-run with "
                "--create-order-row --signal-id <uuid> --order-direction buy|sell)"
            )
            return 3
        else:
            print(
                f"orders row: id={order_row.id} market={order_row.market} "
                f"direction={order_row.direction} quantity={order_row.quantity} "
                f"status={order_row.status}"
            )
            if str(order_row.status) == "filled" and not args.force:
                print("ERROR: order already status='filled' — pass --force to replay anyway")
                return 3
        order_qty = int(order_row.quantity) if order_row is not None else args.fill_quantity
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
