"""scripts/operator_tools/replace_protective_stop.py — POSITION_UNPROTECTED recovery tool.

Operator-invoked tool that places a fresh bracket-stop order for an
existing position whose protective stop was cancelled but never
replaced (the worst-case PR-C failure mode: ``apply_exit_close_placement``
succeeded at the bracket-stop CANCEL step but the close-order PLACE
failed, leaving the position NAKED). Bridges the gap until the next
strategy cycle re-emits the exit (which then re-runs the full
cancel→place pipeline; the bracket placed by this tool gets cancelled
then).

Companion to ``services.audit.event_types.AuditEventType.POSITION_UNPROTECTED``
+ ``services.webhook_pusher.payloads.AlertCategory.POSITION_UNPROTECTED``
(both landed in exit-pipeline PR-C, 2026-05-27). When the operator sees
a POSITION_UNPROTECTED P0 alert in #critical, this is the recovery
runbook.

Lineage
=======

Designed per ``Docs/exit-pipeline-design.md`` §Q5: "Operator-tool: new
``scripts/operator_tools/replace_protective_stop.py`` (PR-5; mirrors
trigger_v1_cycle style — risk-state gate, dry-run default, operator
approval) that places a fresh bracket-stop at the same ATR-derived
level for an existing position." Implementation copies the
``replay_executions.py`` one-shot-IBKR-tool shape: dedicated clientId
in the operator-tool range (80-99), explicit dry-run default, audit-
first ordering, exit-code discipline.

Hard constraints
================

* **Risk-state gate ≠ blocker.** Unlike ``trigger_v1_cycle``, this tool
  is RECOVERY — it runs regardless of ``risk_state.state`` so the
  operator can restore a protective stop during a HALT_NEW (which is
  often the cause: the system halted, an exit signal fired, the close
  placement failed). The current risk state is logged for the operator
  to see, but it does not gate execution.
* **--dry-run default = True.** v1 ships with the safety gate ON; the
  operator must pass ``--no-dry-run`` + ``--confirm`` to actually
  place. Two-flag gate (vs trigger_v1_cycle's one) because this tool
  bypasses the risk-state gate AND touches the live IBKR account.
* **Idempotency.** Before placing, the tool SELECTs orders for an
  existing working bracket-stop on the same position. If one exists,
  abort with ``EXIT_BRACKET_ALREADY_PROTECTED`` (operator can pass
  ``--force`` to override, e.g., to widen the stop level — but the
  default fails closed).
* **Audit-first.** ``ORDER_PLACED`` audit row commits BEFORE
  ``ibkr_client.place_order`` so the intent is durable even if the
  broker call hangs. Mirrors the contract in
  ``services/risk/order_placement_worker.apply_exit_close_placement``.
* **Operator-tool clientId range.** Defaults to 99 (same as
  ``replay_executions.py``); operator passes ``--client-id`` to use a
  different lane (80-99 reserved per
  ``Docs/claude-dev-guide.md`` §1.5). The worker's clientId=1 + bar
  sync's clientId=3 must not collide with this.

.. warning::
   **Cross-clientId cancellation limitation (2026-05-27).** Bracket
   stops placed by this tool will NOT be cancellable by the
   order-placement worker (clientId=1/2) until IBKR Gateway's
   "Master Client ID" is configured to the worker's clientId.
   Without that config, the worker's :func:`cancel_order` finds the
   trade in its local cache (via ``reqAllOpenOrders`` snapshot) and
   calls ``ib.cancelOrder()`` which returns SUCCESS synchronously,
   but IBKR rejects asynchronously with ``Error 10147 "OrderId not
   found"`` — the cancel never lands at the broker. Downstream blast
   radius in the exit-pipeline: the worker thinks the protective
   stop was cancelled, places the closing order, the close fills,
   AND the operator-tool stop remains armed at IBKR — recipe for an
   unintended double-fire or phantom-position scenario.

   **Operator workaround until mitigation lands:** If an exit signal
   fires for a position whose protective stop was placed by this
   tool, manually cancel the operator-tool stop via TWS BEFORE the
   worker's exit cycle runs. The exit pipeline's halted-at-cancel
   step then runs cleanly because the stop is already gone at IBKR.

   See ``Docs/decisions-log.md`` 2026-05-27 entry "Cross-clientId
   IBKR cancellation: structural limitation, mitigation = Master
   Client ID" for the full investigation + the live-cutover
   mitigation candidates (A: Master Client ID; B: post-cancel
   verification in :func:`services.execution.ibkr_adapter.cancel_order`).

Stop-price derivation
=====================

By default the tool reads the original entry's
``signals.sizing_trace["stage_0_universe"]["strategy_inputs"][market]["stop_price"]``
— the same level the original bracket-stop sat at. This preserves the
strategy's ATR-derived stop policy.

Operator can override with ``--stop-price <PRICE>``. Typical reasons:
* Original stop level is no longer protective (market moved through
  it; placing at the original level would fire immediately).
* Operator chooses to widen / tighten the stop based on regime.

Either way, direction-sanity is enforced: for a LONG position the
stop must be STRICTLY BELOW the position's ``avg_cost``; for a SHORT
position, STRICTLY ABOVE. Tick-rounding applies per the market's
PHASE1_TICK_SIZES entry.

Forbidden-paths check
=====================

``scripts/operator_tools/**`` is NOT on the dev-guide §11 anti-pattern
[A02] forbidden-modification whitelist. This script CALLS A02 modules
(``services/execution`` for the IBKR client, ``services/audit`` for
the writer) but does NOT modify them. Regular PR review applies; no
``risk-review-approved`` label needed.

A-gates
=======

* **A01 enforced** — the single audit row (``ORDER_PLACED``) routes
  through ``services.audit.writer.append_audit_event``.
* **A05 enforced** — Decimal at every price boundary; stringified in
  the audit payload + the IBKR adapter coerces back via its A05-clean
  conversion layer.
* **A06 enforced** — every datetime tz-aware UTC.
* **A22 N/A** — unit tests mock the DB + IBKR client; integration
  testcontainers deferred (deferral noted in PR description).
* **A27 satisfied** — operator runbook in
  ``scripts/operator_tools/README.md``.

Exit codes (locked for the operator-runbook contract)
=====================================================

* ``0`` — success: bracket-stop placed (or dry-run printed plan)
* ``1`` — no open position for ``(account, market)``: nothing to
  protect, operator investigates whether the position closed
  out-of-band
* ``2`` — bracket-stop already exists at status='working' (idempotency
  guard); pass ``--force`` to override
* ``3`` — stop-price direction-sanity violation (long stop above
  avg_cost or short stop below); operator passes a sane
  ``--stop-price`` or fixes the upstream sizing_trace
* ``4`` — IBKR connection failure (gateway unreachable or rejected
  the clientId)
* ``5`` — DB init failure (DATABASE_URL missing / wrong)
* ``6`` — invalid CLI args (caught early before any I/O)
* ``7`` — confirmation missing for --no-dry-run (--confirm required)
* ``8`` — broker rejected the stop placement (operator escalates per
  runbook; typically margin / halted market / instrument-permission)
* ``99`` — unexpected exception (logged with traceback)

Usage
=====

::

    python -m scripts.operator_tools.replace_protective_stop \\
        --market /M2K \\
        --env paper \\
        [--stop-price 2750.20] \\
        [--account <UUID>] \\
        [--no-dry-run --confirm]

For the canonical end-to-end command including sops + DATABASE_URL
staging, see ``scripts/operator_tools/README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, append_audit_event
from services.execution.ibkr_adapter import PHASE1_TICK_SIZES, round_to_tick
from services.execution.types import IbkrOrderSide, IbkrPlacementError, IbkrPlaceOrderRequest

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit code contract
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_NO_POSITION: Final[int] = 1
EXIT_BRACKET_ALREADY_PROTECTED: Final[int] = 2
EXIT_DIRECTION_SANITY: Final[int] = 3
EXIT_IBKR_CONNECT_FAILED: Final[int] = 4
EXIT_DB_INIT_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_CONFIRM_MISSING: Final[int] = 7
EXIT_BROKER_REJECTED: Final[int] = 8
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

#: Default operator-tool clientId. Range 80-99 is reserved for one-shot
#: operator tools per ``Docs/claude-dev-guide.md`` §1.5. The worker's
#: clientId=1 + bar_sync's clientId=3 must not collide.
DEFAULT_CLIENT_ID: Final[int] = 99

#: Default ib_gateway host / port — matches docker-compose service name
#: + the paper-account port (4002=live, 4004=paper inside container).
DEFAULT_IBKR_HOST: Final[str] = "ib_gateway"
DEFAULT_IBKR_PORT: Final[int] = 4004

#: Connect timeout for the ib_gateway lazy-connect. Mirror of
#: replay_executions.CONNECT_TIMEOUT_SECONDS.
CONNECT_TIMEOUT_SECONDS: Final[float] = 30.0

#: Hard timeout for the IBKR ``placeOrder`` await — the silent-worker
#: pattern from 2026-05-17 drill 2 also applies here (ib-async can
#: hang indefinitely under broker-side sluggishness).
PLACE_ORDER_TIMEOUT_SECONDS: Final[float] = 30.0

#: Env var the operator stages before invocation.
DATABASE_URL_ENV: Final[str] = "DATABASE_URL"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests use this surface directly without going
    through argparse so they don't have to fight stderr capture."""

    market: str
    env: EnvName
    dry_run: bool
    confirm: bool
    force: bool
    stop_price_override: Decimal | None
    account_id_override: UUID | None
    ibkr_host: str
    ibkr_port: int
    client_id: int
    allow_non_paper: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.replace_protective_stop",
        description=(
            "Place a fresh bracket-stop for an existing position whose "
            "protective stop was cancelled but not replaced (PR-C "
            "POSITION_UNPROTECTED recovery). Mirrors trigger_v1_cycle's "
            "safety shape: --dry-run default, audit-first ordering, "
            "operator-tool clientId range. UNLIKE trigger_v1_cycle, this "
            "tool runs regardless of risk_state (it's recovery, not new "
            "exposure). See scripts/operator_tools/README.md for the "
            "runbook."
        ),
    )
    parser.add_argument(
        "--market",
        required=True,
        help=(
            "Symbol of the position to protect (e.g., '/M2K', 'TLT'). "
            "Must be in PHASE1_TICK_SIZES + match a positions_current "
            "row for the active account."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Env tag for the audit payload — mirrors audit_log.env "
            "CHECK. Live-* envs require --allow-non-paper."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "If set (default), log what WOULD be placed without "
            "touching IBKR. Operator must pass --no-dry-run AND "
            "--confirm to actually place."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "Explicit confirmation for --no-dry-run. Two-flag gate "
            "because this tool bypasses the risk-state gate + touches "
            "the live IBKR account. Fails closed otherwise."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Override the idempotency guard: place a fresh stop even "
            "if a working bracket-stop already exists for this "
            "position. Use to widen / tighten the level. The existing "
            "working stop is NOT cancelled by this tool — operator "
            "cancels it separately via TWS or a follow-up tool."
        ),
    )
    parser.add_argument(
        "--stop-price",
        type=str,
        default=None,
        help=(
            "Stop price override as a string (Decimal-parseable). "
            "When omitted, the tool reads stop_price from the "
            "original entry signal's sizing_trace "
            "(stage_0_universe.strategy_inputs.<market>.stop_price). "
            "Must be strictly below avg_cost for longs and strictly "
            "above for shorts; tick-rounded per the market's "
            "PHASE1_TICK_SIZES entry."
        ),
    )
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help=(
            "Account UUID override. Default resolves to the single "
            "active row from the accounts table (Phase 1 single-"
            "operator). Pass to disambiguate when multiple accounts "
            "are wired (Phase 3+)."
        ),
    )
    parser.add_argument(
        "--ibkr-host",
        default=DEFAULT_IBKR_HOST,
        help=f"ib_gateway host (default: {DEFAULT_IBKR_HOST!r}).",
    )
    parser.add_argument(
        "--ibkr-port",
        type=int,
        default=DEFAULT_IBKR_PORT,
        help=f"ib_gateway port (default: {DEFAULT_IBKR_PORT}).",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=DEFAULT_CLIENT_ID,
        help=(
            f"ib-async clientId (default: {DEFAULT_CLIENT_ID}). MUST "
            "be in 80-99 (operator-tool range per "
            "Docs/claude-dev-guide.md §1.5); collisions with the "
            "worker's clientId=1 OR bar_sync's clientId=3 wedge the "
            "live system."
        ),
    )
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        default=False,
        help=(
            "Required when --env is live-small or live-scale. Fail-"
            "closed default for any non-paper env (tool drives a "
            "broker side-effect)."
        ),
    )
    return parser


def _parse_args(argv: Sequence[str] | None) -> ParsedArgs:
    parser = _build_parser()
    args = parser.parse_args(argv)

    env: EnvName = args.env
    if env in ("live-small", "live-scale") and not args.allow_non_paper:
        parser.error(
            f"--env={env!r} requires --allow-non-paper (fail-closed default "
            "because this tool drives broker side-effects in the live "
            "account)."
        )

    if not args.dry_run and not args.confirm:
        parser.error(
            "--no-dry-run requires --confirm (two-flag gate because this "
            "tool bypasses the risk-state gate + places a real order)."
        )

    # Operator-tool clientId range gate. Reserve 1-7 for live worker
    # lanes; allow 80-99 for one-shots.
    if not (80 <= args.client_id <= 99):
        parser.error(
            f"--client-id={args.client_id} must be in 80-99 (operator-tool "
            "range per Docs/claude-dev-guide.md §1.5). Collisions with the "
            "worker (clientId=1) or bar_sync (clientId=3) wedge IBKR."
        )

    stop_price_override: Decimal | None = None
    if args.stop_price is not None:
        try:
            stop_price_override = Decimal(args.stop_price)
        except InvalidOperation:
            parser.error(f"--stop-price={args.stop_price!r} is not Decimal-parseable")
        if stop_price_override <= 0:
            parser.error(f"--stop-price={args.stop_price!r} must be > 0")

    account_id_override: UUID | None = None
    if args.account is not None:
        try:
            account_id_override = UUID(args.account)
        except ValueError:
            parser.error(f"--account={args.account!r} is not a valid UUID")

    if not args.market.strip():
        parser.error("--market must be non-empty")

    return ParsedArgs(
        market=args.market,
        env=env,
        dry_run=args.dry_run,
        confirm=args.confirm,
        force=args.force,
        stop_price_override=stop_price_override,
        account_id_override=account_id_override,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port,
        client_id=args.client_id,
        allow_non_paper=args.allow_non_paper,
    )


# ---------------------------------------------------------------------------
# Plan dataclasses (pure-policy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenPositionSnapshot:
    """The currently-held position the tool is protecting."""

    position_id: UUID
    account_id: UUID
    market: str
    contract_id: UUID | None
    quantity: int
    """Signed magnitude. Long = positive, short = negative."""
    avg_cost: Decimal


@dataclass(frozen=True, slots=True)
class OpenTradeSnapshot:
    """The open trade row that opened the position (for entry-order
    linkage + strategy/parameter hash propagation to the new stop's
    audit row)."""

    trade_id: UUID
    entry_signal_id: UUID
    entry_order_id: UUID
    strategy_hash: str
    parameter_set_hash: str
    slippage_calibration_version_id: UUID


@dataclass(frozen=True, slots=True)
class ReplaceProtectiveStopPlan:
    """Pure-policy result of :func:`plan_replace_protective_stop`.

    Captures everything the apply step needs to: (a) write the
    ORDER_PLACED audit row, (b) call IBKR's placeOrder for a fresh
    stop_market, (c) INSERT the orders row.

    All ID derivation deterministic per the existing
    ``_build_client_order_id`` pattern (with a ``-stop-replace-N``
    suffix to avoid colliding with the original entry's CID + the
    cancelled bracket-stop's ``-stop`` CID).
    """

    account_id: UUID
    env: Environment
    market: str
    contract_id: UUID | None
    entry_signal_id: UUID
    entry_order_id: UUID
    stop_side: IbkrOrderSide
    stop_quantity: Decimal
    """Magnitude only (positive)."""
    stop_price: Decimal
    stop_client_order_id: str
    prior_position_direction: Literal["long", "short"]
    avg_cost: Decimal
    strategy_hash: str
    parameter_set_hash: str
    stop_source: Literal["sizing_trace", "operator_override"]


# ---------------------------------------------------------------------------
# Pure-policy planner
# ---------------------------------------------------------------------------


def _build_replace_stop_client_order_id(
    *,
    strategy_hash: str,
    parameter_set_hash: str,
    entry_signal_id: UUID,
    retry_n: int,
) -> str:
    """Build the client_order_id for a replacement bracket-stop.

    Format: ``<strat8>-<param8>-<sig8>-stop-replace-<retry_n>``. The
    ``stop-replace`` segment distinguishes from the original entry
    (``<strat8>-<param8>-<sig8>-0``) + the original bracket-stop
    (``<strat8>-<param8>-<sig8>-0-stop``). retry_n is 0..9 (single
    digit) to keep CID length sane; operator can re-run the tool with
    a different ``--stop-price`` and pick up retry_n=1 if a prior
    replacement is still working.
    """
    if len(strategy_hash) < 8:
        raise ValueError(f"strategy_hash {strategy_hash!r} too short; need ≥ 8 chars")
    if len(parameter_set_hash) < 8:
        raise ValueError(f"parameter_set_hash {parameter_set_hash!r} too short; need ≥ 8 chars")
    if not (0 <= retry_n <= 9):
        raise ValueError(f"retry_n must be 0..9; got {retry_n}")
    sig_hex = entry_signal_id.hex
    return f"{strategy_hash[:8]}-{parameter_set_hash[:8]}-{sig_hex[:8]}-stop-replace-{retry_n}"


def _extract_stop_price_from_sizing_trace(
    sizing_trace: dict[str, Any],
    *,
    market: str,
) -> Decimal | None:
    """Pull the original ATR-derived stop_price out of the entry signal's
    sizing_trace. Returns ``None`` when the canonical path is missing
    (operator must pass ``--stop-price`` explicitly in that case).
    """
    try:
        stage_0 = sizing_trace["stage_0_universe"]
        strategy_inputs = stage_0["strategy_inputs"]
        market_inputs = strategy_inputs[market]
        raw_stop = market_inputs["stop_price"]
    except (KeyError, TypeError):
        return None
    if raw_stop is None or raw_stop == "":
        return None
    try:
        return Decimal(str(raw_stop))
    except (ArithmeticError, ValueError):
        return None


class ReplaceProtectiveStopError(Exception):
    """Raised for terminal-failure plan construction."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def plan_replace_protective_stop(
    *,
    position: OpenPositionSnapshot,
    trade: OpenTradeSnapshot,
    sizing_trace: dict[str, Any],
    stop_price_override: Decimal | None,
    env: Environment,
    retry_n: int = 0,
) -> ReplaceProtectiveStopPlan:
    """Build the placement plan for a replacement bracket-stop.

    Pure policy — no I/O, no clock, no random. Same inputs → same plan.

    Stop-price source:

    * ``stop_price_override`` set → use that, source='operator_override'
    * ``stop_price_override`` None → pull from sizing_trace;
      source='sizing_trace'
    * Both missing → raise ``ReplaceProtectiveStopError`` (operator
      must pass --stop-price)

    Direction-sanity:

    * Long position (quantity > 0): stop_price MUST be strictly below
      avg_cost (a sell-stop above avg_cost would fire immediately)
    * Short position (quantity < 0): stop_price MUST be strictly above
      avg_cost (a buy-stop below avg_cost would fire immediately)

    Tick-rounding applied after the direction check on the raw price,
    then re-validated post-rounding (a price that's 1 tick inside the
    danger zone can flip past after rounding).
    """
    if position.quantity == 0:
        raise ReplaceProtectiveStopError(
            error_code="POSITION_FLAT",
            message=(f"position quantity is 0 for market={position.market}; nothing to protect"),
        )

    direction: Literal["long", "short"] = "long" if position.quantity > 0 else "short"
    stop_side: IbkrOrderSide = "sell" if direction == "long" else "buy"
    stop_qty = Decimal(abs(position.quantity))

    # Resolve raw stop price + source
    stop_source: Literal["sizing_trace", "operator_override"]
    raw_stop_price: Decimal | None
    if stop_price_override is not None:
        raw_stop_price = stop_price_override
        stop_source = "operator_override"
    else:
        raw_stop_price = _extract_stop_price_from_sizing_trace(sizing_trace, market=position.market)
        stop_source = "sizing_trace"
        if raw_stop_price is None:
            raise ReplaceProtectiveStopError(
                error_code="STOP_PRICE_UNAVAILABLE",
                message=(
                    "Original entry signal's sizing_trace does not carry "
                    f"stage_0_universe.strategy_inputs.{position.market}."
                    "stop_price; operator must pass --stop-price."
                ),
            )

    # Pre-rounding direction sanity
    if direction == "long" and raw_stop_price >= position.avg_cost:
        raise ReplaceProtectiveStopError(
            error_code="STOP_DIRECTION_INVALID",
            message=(
                f"Long position avg_cost={position.avg_cost} but stop_price="
                f"{raw_stop_price} is NOT strictly below — would fire "
                "immediately. Pass --stop-price below the avg cost."
            ),
        )
    if direction == "short" and raw_stop_price <= position.avg_cost:
        raise ReplaceProtectiveStopError(
            error_code="STOP_DIRECTION_INVALID",
            message=(
                f"Short position avg_cost={position.avg_cost} but stop_price="
                f"{raw_stop_price} is NOT strictly above — would fire "
                "immediately. Pass --stop-price above the avg cost."
            ),
        )

    # Tick-round
    try:
        stop_price = round_to_tick(raw_stop_price, market=position.market)
    except KeyError as exc:
        raise ReplaceProtectiveStopError(
            error_code="UNKNOWN_MARKET_TICK",
            message=(
                f"market {position.market!r} not in PHASE1_TICK_SIZES; see "
                "services.execution.ibkr_adapter for the Phase 1 universe map."
            ),
        ) from exc

    # Post-rounding direction sanity (rounding can flip past in edges)
    if direction == "long" and stop_price >= position.avg_cost:
        raise ReplaceProtectiveStopError(
            error_code="STOP_DIRECTION_INVALID",
            message=(
                f"After tick-rounding: long avg_cost={position.avg_cost} but "
                f"stop_price={stop_price} (rounded from {raw_stop_price}) is "
                f"NOT strictly below. Tick="
                f"{PHASE1_TICK_SIZES.get(position.market)}; widen the stop."
            ),
        )
    if direction == "short" and stop_price <= position.avg_cost:
        raise ReplaceProtectiveStopError(
            error_code="STOP_DIRECTION_INVALID",
            message=(
                f"After tick-rounding: short avg_cost={position.avg_cost} but "
                f"stop_price={stop_price} (rounded from {raw_stop_price}) is "
                f"NOT strictly above. Tick="
                f"{PHASE1_TICK_SIZES.get(position.market)}; widen the stop."
            ),
        )

    cid = _build_replace_stop_client_order_id(
        strategy_hash=trade.strategy_hash,
        parameter_set_hash=trade.parameter_set_hash,
        entry_signal_id=trade.entry_signal_id,
        retry_n=retry_n,
    )

    return ReplaceProtectiveStopPlan(
        account_id=position.account_id,
        env=env,
        market=position.market,
        contract_id=position.contract_id,
        entry_signal_id=trade.entry_signal_id,
        entry_order_id=trade.entry_order_id,
        stop_side=stop_side,
        stop_quantity=stop_qty,
        stop_price=stop_price,
        stop_client_order_id=cid,
        prior_position_direction=direction,
        avg_cost=position.avg_cost,
        strategy_hash=trade.strategy_hash,
        parameter_set_hash=trade.parameter_set_hash,
        stop_source=stop_source,
    )


# ---------------------------------------------------------------------------
# I/O lookup helpers
# ---------------------------------------------------------------------------


async def fetch_active_account_id(
    session_factory: async_sessionmaker[Any],
) -> UUID | None:
    """Read the single active accounts row (Phase 1 single-operator)."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM accounts WHERE active_to IS NULL "
                    "ORDER BY active_from ASC LIMIT 1"
                )
            )
        ).fetchone()
    if row is None:
        return None
    return UUID(str(row.id))


async def fetch_open_position(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    market: str,
) -> OpenPositionSnapshot | None:
    """SELECT positions_current for (account, market). Returns None when
    no row exists (position already flat → nothing to protect)."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, account_id, market, contract_id, quantity, "
                    "       avg_cost "
                    "FROM positions_current "
                    "WHERE account_id = :acct AND market = :market"
                ),
                {"acct": account_id, "market": market},
            )
        ).fetchone()
    if row is None:
        return None
    return OpenPositionSnapshot(
        position_id=UUID(str(row.id)),
        account_id=UUID(str(row.account_id)),
        market=row.market,
        contract_id=UUID(str(row.contract_id)) if row.contract_id is not None else None,
        quantity=int(row.quantity),
        avg_cost=Decimal(str(row.avg_cost)),
    )


async def fetch_open_trade(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    market: str,
) -> OpenTradeSnapshot | None:
    """SELECT trades.state='open_position' for (account, market)."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, entry_signal_id, entry_order_id, "
                    "       strategy_hash, parameter_set_hash, "
                    "       slippage_calibration_version_id "
                    "FROM trades "
                    "WHERE account_id = :acct AND market = :market "
                    "  AND state = 'open_position' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"acct": account_id, "market": market},
            )
        ).fetchone()
    if row is None:
        return None
    return OpenTradeSnapshot(
        trade_id=UUID(str(row.id)),
        entry_signal_id=UUID(str(row.entry_signal_id)),
        entry_order_id=UUID(str(row.entry_order_id)),
        strategy_hash=row.strategy_hash,
        parameter_set_hash=row.parameter_set_hash,
        slippage_calibration_version_id=UUID(str(row.slippage_calibration_version_id)),
    )


async def fetch_sizing_trace(
    session_factory: async_sessionmaker[Any],
    *,
    signal_id: UUID,
) -> dict[str, Any] | None:
    """Pull the entry signal's sizing_trace JSONB column."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT sizing_trace FROM signals WHERE id = :sid"),
                {"sid": signal_id},
            )
        ).fetchone()
    if row is None or row.sizing_trace is None:
        return None
    return dict(row.sizing_trace)


async def fetch_existing_working_bracket_stop(
    session_factory: async_sessionmaker[Any],
    *,
    entry_signal_id: UUID,
) -> str | None:
    """Idempotency probe: is there ALREADY a working stop_market order
    for this entry signal? Returns the existing stop's client_order_id
    so the caller can report it; ``None`` if no such order exists."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT client_order_id FROM orders "
                    "WHERE signal_id = :sid AND order_type = 'stop_market' "
                    "  AND status = 'working' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"sid": entry_signal_id},
            )
        ).fetchone()
    if row is None:
        return None
    return str(row.client_order_id)


# ---------------------------------------------------------------------------
# I/O orchestrator
# ---------------------------------------------------------------------------


async def apply_replace_protective_stop(
    plan: ReplaceProtectiveStopPlan,
    *,
    ibkr_client: Any,
    contract: Any,
    session_factory: async_sessionmaker[Any],
    phase_at_emit: Literal[0, 1, 2, 3] = 1,
) -> dict[str, Any]:
    """Execute the plan with audit-first ordering.

    Sequence:
    1. ORDER_PLACED audit (intent — commits before the IBKR call)
    2. PR-ζ pre-INSERT orders row (status='pending', broker_order_id=NULL,
       parent_order_id = the ORIGINAL entry order id)
    3. ibkr_client.place_order(stop_request)
    4. UPDATE orders row with broker_order_id + status

    Returns a dict suitable for structlog observability + the operator's
    runbook ack.
    """
    placed_at = datetime.now(tz=UTC)

    # Step 1: audit-first ORDER_PLACED
    audit_payload: dict[str, Any] = {
        "signal_id": str(plan.entry_signal_id),
        "account_id": str(plan.account_id),
        "client_order_id": plan.stop_client_order_id,
        "broker_order_id": None,  # filled in by the apply post-place
        "market": plan.market,
        "side": plan.stop_side,
        "quantity": str(plan.stop_quantity),
        "order_type": "stop_market",
        "stop_price": str(plan.stop_price),
        "time_in_force": "GTC",
        "placed_at_utc": placed_at.isoformat(),
        "leg": "operator_replace_protective_stop",
        "stop_source": plan.stop_source,
        "prior_position_direction": plan.prior_position_direction,
        "avg_cost": str(plan.avg_cost),
        "reason": "operator_replace_protective_stop",
    }
    async with session_factory() as audit_session:
        audit_record = await append_audit_event(
            audit_session,
            AuditEventType.ORDER_PLACED,
            audit_payload,
            account_id=plan.account_id,
            env=plan.env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=placed_at,
        )

    # Step 2: PR-ζ pre-INSERT orders row
    async with session_factory() as session:
        async with session.begin():
            inserted = (
                await session.execute(
                    text(
                        """
                        INSERT INTO orders (
                            account_id, env, signal_id, client_order_id,
                            broker_order_id, market, direction, order_type,
                            quantity, stop_price, placed_at_utc, status,
                            rejection_reason, retry_n, parent_order_id,
                            strategy_hash, parameter_set_hash
                        ) VALUES (
                            :acct, :env, :sig, :cid,
                            NULL, :market, :side, 'stop_market',
                            :qty, :stop_px, :placed_at, 'pending',
                            NULL, 0, :parent_id,
                            :strat, :param
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "acct": plan.account_id,
                        "env": plan.env,
                        "sig": plan.entry_signal_id,
                        "cid": plan.stop_client_order_id,
                        "market": plan.market,
                        "side": plan.stop_side,
                        "qty": int(plan.stop_quantity),
                        "stop_px": plan.stop_price,
                        "placed_at": placed_at,
                        "parent_id": plan.entry_order_id,
                        "strat": plan.strategy_hash,
                        "param": plan.parameter_set_hash,
                    },
                )
            ).fetchone()
            assert inserted is not None
            new_order_id: UUID = inserted.id

    # Step 3: IBKR place_order
    stop_request = IbkrPlaceOrderRequest(
        client_order_id=plan.stop_client_order_id,
        contract=contract,
        side=plan.stop_side,
        quantity=plan.stop_quantity,
        order_type="stop_market",
        stop_price=plan.stop_price,
        time_in_force="GTC",
        # No parent — the original entry has long since filled; this is
        # a standalone protective stop.
        transmit=True,
    )

    try:
        broker_result = await asyncio.wait_for(
            ibkr_client.place_order(stop_request),
            timeout=PLACE_ORDER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise IbkrPlacementError(
            operation="placeOrder.replace_protective_stop",
            detail=f"timed out after {PLACE_ORDER_TIMEOUT_SECONDS}s",
            underlying_exception_class="TimeoutError",
            occurred_at_utc=datetime.now(tz=UTC),
        ) from exc

    broker_status = broker_result.status
    broker_order_id = broker_result.broker_order_id
    rejection_detail = getattr(broker_result, "rejection_detail", None)
    db_status = _broker_status_to_orders_status(broker_status)

    # Step 4: UPDATE orders row
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE orders SET broker_order_id = :bid, status = :status, "
                    "    rejection_reason = :rej_reason WHERE id = :oid"
                ),
                {
                    "bid": str(broker_order_id) if broker_order_id is not None else None,
                    "status": db_status,
                    "rej_reason": rejection_detail,
                    "oid": new_order_id,
                },
            )

    log.info(
        "replace_protective_stop_completed",
        order_id=str(new_order_id),
        client_order_id=plan.stop_client_order_id,
        broker_order_id=str(broker_order_id) if broker_order_id is not None else None,
        broker_status=broker_status,
        db_status=db_status,
        rejection_detail=rejection_detail,
        audit_event_uuid=str(audit_record.event_uuid),
        market=plan.market,
        side=plan.stop_side,
        quantity=str(plan.stop_quantity),
        stop_price=str(plan.stop_price),
        stop_source=plan.stop_source,
    )

    return {
        "order_id": str(new_order_id),
        "broker_order_id": (str(broker_order_id) if broker_order_id is not None else None),
        "broker_status": broker_status,
        "db_status": db_status,
        "audit_event_uuid": str(audit_record.event_uuid),
        "rejection_detail": rejection_detail,
    }


def _broker_status_to_orders_status(broker_status: str) -> str:
    """Mirror of order_placement_worker._broker_status_to_orders_status.

    Duplicated here (not imported) so this script's import surface
    stays minimal — pulling in the worker module would also pull SSE
    multiplexer + the audit writer's deeper transitive deps.
    """
    if broker_status in ("submitted", "working"):
        return "working"
    if broker_status == "pending_submit":
        return "pending"
    if broker_status == "rejected":
        return "rejected"
    if broker_status in ("filled", "partially_filled", "cancelled", "expired"):
        return broker_status
    return "pending"


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------


async def _amain(args: ParsedArgs) -> int:
    log.info(
        "replace_protective_stop_started",
        market=args.market,
        env=args.env,
        dry_run=args.dry_run,
        force=args.force,
        client_id=args.client_id,
        stop_price_override=(
            str(args.stop_price_override) if args.stop_price_override is not None else None
        ),
    )

    # Init DB pool
    database_url = os.environ.get(DATABASE_URL_ENV)
    if database_url is None or not database_url.strip():
        log.error(
            "replace_protective_stop_db_url_missing",
            note=f"set {DATABASE_URL_ENV} env var before invocation",
        )
        return EXIT_DB_INIT_FAILED

    engine = None
    try:
        engine = create_async_engine(database_url, future=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
    except Exception as exc:
        log.error("replace_protective_stop_db_init_failed", error=str(exc))
        return EXIT_DB_INIT_FAILED

    try:
        # Resolve account
        account_id = args.account_id_override
        if account_id is None:
            account_id = await fetch_active_account_id(session_factory)
            if account_id is None:
                log.error(
                    "replace_protective_stop_no_active_account",
                    note=(
                        "no accounts row with active_to IS NULL; complete "
                        "/setup or pass --account explicitly"
                    ),
                )
                return EXIT_DB_INIT_FAILED

        # Resolve position
        position = await fetch_open_position(
            session_factory, account_id=account_id, market=args.market
        )
        if position is None:
            log.error(
                "replace_protective_stop_no_open_position",
                market=args.market,
                account_id=str(account_id),
                note=(
                    "no positions_current row for (account, market); the "
                    "position may have closed out-of-band or the operator "
                    "supplied the wrong market"
                ),
            )
            return EXIT_NO_POSITION
        if position.quantity == 0:
            log.error(
                "replace_protective_stop_position_flat",
                market=args.market,
                note="positions_current.quantity is 0; nothing to protect",
            )
            return EXIT_NO_POSITION

        # Resolve trade + sizing_trace
        trade = await fetch_open_trade(session_factory, account_id=account_id, market=args.market)
        if trade is None:
            log.error(
                "replace_protective_stop_no_open_trade",
                market=args.market,
                note=(
                    "positions_current exists but no trades.state='open_position' "
                    "row matches — schema drift; operator must reconcile manually"
                ),
            )
            return EXIT_NO_POSITION

        # Idempotency guard
        if not args.force:
            existing_cid = await fetch_existing_working_bracket_stop(
                session_factory, entry_signal_id=trade.entry_signal_id
            )
            if existing_cid is not None:
                log.error(
                    "replace_protective_stop_bracket_already_protected",
                    market=args.market,
                    existing_stop_client_order_id=existing_cid,
                    note=(
                        "a working bracket-stop already exists for this "
                        "position; pass --force to add another"
                    ),
                )
                return EXIT_BRACKET_ALREADY_PROTECTED

        sizing_trace = (
            await fetch_sizing_trace(session_factory, signal_id=trade.entry_signal_id) or {}
        )

        # Build plan
        try:
            plan = plan_replace_protective_stop(
                position=position,
                trade=trade,
                sizing_trace=sizing_trace,
                stop_price_override=args.stop_price_override,
                env=args.env,
            )
        except ReplaceProtectiveStopError as exc:
            log.error(
                "replace_protective_stop_plan_error",
                error_code=exc.error_code,
                error=str(exc),
            )
            if exc.error_code == "STOP_DIRECTION_INVALID":
                return EXIT_DIRECTION_SANITY
            if exc.error_code == "UNKNOWN_MARKET_TICK":
                return EXIT_DIRECTION_SANITY  # treat as sanity error
            return EXIT_BAD_ARGS  # STOP_PRICE_UNAVAILABLE / POSITION_FLAT

        log.info(
            "replace_protective_stop_plan_built",
            market=plan.market,
            stop_side=plan.stop_side,
            stop_quantity=str(plan.stop_quantity),
            stop_price=str(plan.stop_price),
            stop_client_order_id=plan.stop_client_order_id,
            stop_source=plan.stop_source,
            prior_position_direction=plan.prior_position_direction,
            avg_cost=str(plan.avg_cost),
            entry_signal_id=str(plan.entry_signal_id),
            entry_order_id=str(plan.entry_order_id),
        )

        if args.dry_run:
            log.info(
                "replace_protective_stop_dry_run_complete",
                note=(
                    "no IBKR call made; no DB writes. Re-run with --no-dry-run --confirm to apply."
                ),
            )
            return EXIT_OK

        # Real IBKR path
        # Lazy import — keeps the dry-run path free of ib_async dependency
        from services.execution.ibkr_adapter import IbAsyncIbkrClient

        ibkr_client = IbAsyncIbkrClient(
            host=args.ibkr_host,
            port=args.ibkr_port,
            account_id=None,  # use default account on session
            client_id=args.client_id,
        )
        try:
            await asyncio.wait_for(ibkr_client.connect(), timeout=CONNECT_TIMEOUT_SECONDS)
        except IbkrPlacementError as exc:
            log.error("replace_protective_stop_ibkr_connect_failed", error=str(exc))
            return EXIT_IBKR_CONNECT_FAILED
        except TimeoutError:
            log.error(
                "replace_protective_stop_ibkr_connect_timeout",
                timeout_s=CONNECT_TIMEOUT_SECONDS,
            )
            return EXIT_IBKR_CONNECT_FAILED

        try:
            contract = await asyncio.wait_for(
                ibkr_client.resolve_contract(plan.market),
                timeout=PLACE_ORDER_TIMEOUT_SECONDS,
            )
        except (IbkrPlacementError, TimeoutError) as exc:
            log.error(
                "replace_protective_stop_resolve_contract_failed",
                market=plan.market,
                error=str(exc),
            )
            try:
                await ibkr_client.disconnect()
            except Exception:
                pass
            return EXIT_IBKR_CONNECT_FAILED

        try:
            result = await apply_replace_protective_stop(
                plan,
                ibkr_client=ibkr_client,
                contract=contract,
                session_factory=session_factory,
            )
        except IbkrPlacementError as exc:
            log.error("replace_protective_stop_broker_call_failed", error=str(exc))
            return EXIT_BROKER_REJECTED
        finally:
            try:
                await ibkr_client.disconnect()
            except Exception as exc:
                log.warning("replace_protective_stop_disconnect_error", error=str(exc))

        if result["db_status"] == "rejected":
            log.error(
                "replace_protective_stop_broker_rejected",
                rejection_detail=result["rejection_detail"],
                client_order_id=plan.stop_client_order_id,
            )
            return EXIT_BROKER_REJECTED

        return EXIT_OK
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log.warning("replace_protective_stop_db_dispose_error", error=str(exc))


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Sync entry point. Returns exit code; never sys.exits."""
    _configure_logging()
    try:
        try:
            args = _parse_args(argv)
        except SystemExit as exc:
            return EXIT_BAD_ARGS if exc.code != 0 else EXIT_OK
        return asyncio.run(_amain(args))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "DATABASE_URL_ENV",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_IBKR_HOST",
    "DEFAULT_IBKR_PORT",
    "EXIT_BAD_ARGS",
    "EXIT_BRACKET_ALREADY_PROTECTED",
    "EXIT_BROKER_REJECTED",
    "EXIT_CONFIRM_MISSING",
    "EXIT_DB_INIT_FAILED",
    "EXIT_DIRECTION_SANITY",
    "EXIT_IBKR_CONNECT_FAILED",
    "EXIT_NO_POSITION",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "PLACE_ORDER_TIMEOUT_SECONDS",
    "OpenPositionSnapshot",
    "OpenTradeSnapshot",
    "ParsedArgs",
    "ReplaceProtectiveStopError",
    "ReplaceProtectiveStopPlan",
    "_amain",
    "_broker_status_to_orders_status",
    "_build_parser",
    "_build_replace_stop_client_order_id",
    "_extract_stop_price_from_sizing_trace",
    "_parse_args",
    "apply_replace_protective_stop",
    "fetch_active_account_id",
    "fetch_existing_working_bracket_stop",
    "fetch_open_position",
    "fetch_open_trade",
    "fetch_sizing_trace",
    "main",
    "plan_replace_protective_stop",
]
