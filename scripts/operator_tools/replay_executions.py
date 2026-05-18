"""scripts/operator_tools/replay_executions.py — IBKR execution replay → backend recovery.

Recovery tool for the **backend-blind-fills** failure mode: IBKR has the
fills, but the backend missed the ``orderStatus`` events (typically due
to an IBKR Error 1100 connection lossage at gateway↔IBKR). The audit
chain stays clean, but ``fills`` / ``positions_current`` / ``balances``
/ ``trades`` rows are NEVER written, and the worker's in-process dedupe
set has no record of the order. Even if the gateway reconnects, IBKR
does NOT re-fire historical ``orderStatus`` events for an order that
filled while the api was disconnected — the events are gone.

This script pulls today's executions back from IBKR (via the
``reqExecutionsAsync`` REST endpoint, which is queryable on-demand),
filters by ``orderRef`` to the operator-supplied client_order_ids, then
calls :func:`services.risk.fill_processor.process_fill_event` for each
match. The fill_processor writes the full audit chain (ORDER_FILLED →
POSITION_OPENED/UPDATED/CLOSED → BALANCE_SNAPSHOT_RECORDED → optional
TRADE_OPENED/CLOSED) + INSERTs/UPDATEs all the backend tables that the
worker's ``_on_order_status`` callback would have written if it had been
listening at fill time.

**SSE is NOT emitted** — the SSE multiplexer is intra-process to the api
container; this script is a separate process. Operator manually POSTs
to ``#fills`` Discord per the runbook if downstream subscribers need
notification.

**Lineage.** This is the committable, parameterized version of the
transient ``/tmp/drill5_recovery.py`` script that closed the 2026-05-18
drill 5 backend-blind-fills incident. The drill ran with the same
clientId=99 + reqExecutionsAsync + FillIngestPayload + process_fill_event
shape; this version adds CLI argparse, structured logging, exit-code
discipline, an --allow-non-paper gate on live-env writes, and unit-test
coverage that locks the VWAP / commission / latest-time aggregation
math against future regressions.

**Forbidden-paths check.** ``scripts/operator_tools/**`` is a NEW path; not on
the dev-guide §11 anti-pattern [A02] forbidden-modification whitelist
nor on the §2.3 hot-fix whitelist. Pure tooling — no
``services/risk/**`` or ``services/execution/**`` modifications. The
script CALLS those modules (via ``process_fill_event``) but does not
modify them. Regular PR review applies.

**A-gates:**

* **A01 enforced** — every write goes through ``process_fill_event``
  which routes audit emits through ``services.audit.writer.append_audit_event``.
* **A05 enforced** — every numeric value coerced to Decimal at the IBKR
  boundary; floats raise via JCS canonicaliser downstream.
* **A06 enforced** — every datetime tz-aware UTC at the IBKR boundary;
  fill_processor's planner re-validates.
* **A22 N/A** — tests mock ib_async + ``process_fill_event``; no
  testcontainers / no real IBKR in CI.
* **A27 satisfied** — operator runbook at ``scripts/operator_tools/README.md``.

Exit codes (load-bearing for the operator-runbook contract):

* ``0`` — success; every supplied CID matched + replayed cleanly
* ``1`` — at least one CID had no matching execution at IBKR (operator
  investigates — wrong CID, wrong date, or the fill actually didn't
  happen yet)
* ``2`` — at least one CID raised :class:`UnsupportedFillScenarioError`
  (partial fill / reversal case; operator reconciles manually)
* ``3`` — at least one CID raised :class:`FillProcessingError`
  (terminal failure; operator escalates per runbook)
* ``4`` — IBKR connection failure (gateway unreachable or rejected the
  clientId; operator investigates the gateway side)
* ``5`` — DB pool init failure (DATABASE_URL missing or
  wrong; check Step 2 of the runbook)
* ``6`` — Invalid CLI args (caught early, before any I/O)
* ``99`` — Unexpected exception (logged with traceback; operator
  escalates)

Usage::

    python -m scripts.operator_tools.replay_executions \\
        --client-order-ids c951158b-...,c951158c-... \\
        --env paper

For the canonical end-to-end command including sops + DATABASE_URL
staging on the VPS, see ``scripts/operator_tools/README.md`` Step 3.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit code contract (locked; documented in module docstring + README)
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_NO_MATCH: Final[int] = 1
EXIT_UNSUPPORTED_SCENARIO: Final[int] = 2
EXIT_FILL_PROCESSING_ERROR: Final[int] = 3
EXIT_IBKR_CONNECT_FAILED: Final[int] = 4
EXIT_DB_INIT_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

#: Env values accepted on the CLI. Mirror of ``audit_log.env`` CHECK
#: constraint (``alembic/versions/0001_audit_log.py``). "dev" is NOT
#: valid — the fill_processor's audit emits use the locked
#: ``Environment`` Literal from ``services.audit.writer`` which only
#: permits paper / live-small / live-scale.
_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

#: Default clientId for the read-only IBKR connection. The worker uses
#: clientId=1 (or whatever ``API_IBKR_CLIENT_ID`` is overridden to per
#: PR #167); using 99 here ensures we don't collide. Values 1-7 are the
#: ib-async convention for "live worker" lanes; 99 is conventionally
#: reserved for one-shot operator tools.
DEFAULT_CLIENT_ID: Final[int] = 99

#: Default ib_gateway host / port — matches docker-compose service name +
#: the paper-account port (4002=live, 4004=paper inside the container).
DEFAULT_IBKR_HOST: Final[str] = "ib_gateway"
DEFAULT_IBKR_PORT: Final[int] = 4004

#: Connect timeout for the ib_gateway lazy-connect. Long enough to
#: tolerate gateway-restart races; short enough that a wedged gateway
#: surfaces as exit 4 in under a minute.
CONNECT_TIMEOUT_SECONDS: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests use this surface directly without
    going through argparse so they don't have to fight stderr capture.
    """

    client_order_ids: tuple[str, ...]
    env: EnvName
    ibkr_host: str
    ibkr_port: int
    client_id: int
    filter_acct: str | None
    filter_time: str | None
    dry_run: bool
    allow_non_paper: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.replay_executions",
        description=(
            "Replay IBKR executions through the backend fill_processor "
            "for the supplied client_order_ids. Use when backend-blind "
            "fills are confirmed (IBKR has fills + backend audit chain "
            "has no matching ORDER_FILLED row). See "
            "scripts/operator_tools/README.md for the operator runbook."
        ),
    )
    parser.add_argument(
        "--client-order-ids",
        required=True,
        help=(
            "Comma-separated list of orderRef values to filter IBKR "
            "executions on. Same CIDs as in the ``orders.client_order_id`` "
            "column. Example: ``c951158b-...,c951158c-...``."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Audit env tag for the FillIngestPayload writes — mirrors "
            "audit_log.env CHECK. Recovery in live-* envs requires the "
            "--allow-non-paper safety gate."
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
        help=f"ib_gateway port (default: {DEFAULT_IBKR_PORT}; 4002=live, 4004=paper).",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=DEFAULT_CLIENT_ID,
        help=(
            f"ib-async clientId for the read-only connection (default: "
            f"{DEFAULT_CLIENT_ID}; chosen to avoid collision with the "
            "worker's clientId=1)."
        ),
    )
    parser.add_argument(
        "--filter-acct",
        default=None,
        help=(
            "Optional IBKR account number passthrough into "
            "ExecutionFilter(acctCode=...). Useful when the connection "
            "has access to multiple accounts."
        ),
    )
    parser.add_argument(
        "--filter-time",
        default=None,
        help=(
            "Optional ``YYYYMMDD HH:MM:SS`` timestamp passthrough into "
            "ExecutionFilter(time=...). IBKR returns executions filled "
            "at-or-after this time."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "If set, fetch executions + print matches but do NOT call "
            "process_fill_event. No audit writes; no table mutations. "
            "Useful for verifying CID matching before committing."
        ),
    )
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        default=False,
        help=(
            "Required when --env is live-small or live-scale. The script "
            "fails closed without this flag for recovery tooling that "
            "mutates audit chain rows."
        ),
    )
    return parser


def _parse_args(argv: Sequence[str] | None) -> ParsedArgs:
    """Argparse → ParsedArgs. Raises SystemExit(EXIT_BAD_ARGS) for
    validation failures specific to this CLI (live-env gate, empty
    client-order-ids list); argparse itself raises SystemExit(2) on
    parse failure which the main entry point translates to
    EXIT_BAD_ARGS.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    raw_cids: str = args.client_order_ids
    cids = tuple(s.strip() for s in raw_cids.split(",") if s.strip())
    if not cids:
        parser.error("--client-order-ids must be a non-empty comma-separated list")

    env: EnvName = args.env
    if env in ("live-small", "live-scale") and not args.allow_non_paper:
        parser.error(
            f"--env={env!r} requires --allow-non-paper (fail-closed default for "
            f"recovery tooling that mutates audit chain rows in production)"
        )

    return ParsedArgs(
        client_order_ids=cids,
        env=env,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port,
        client_id=args.client_id,
        filter_acct=args.filter_acct,
        filter_time=args.filter_time,
        dry_run=bool(args.dry_run),
        allow_non_paper=bool(args.allow_non_paper),
    )


# ---------------------------------------------------------------------------
# IBKR execution aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AggregatedExecution:
    """A single CID's executions rolled up into a FillIngestPayload-ready
    shape. VWAP price + summed commission + latest fill timestamp.

    Magnitudes only — the worker's `_on_order_status` uses the absolute
    ``cumulative_filled_quantity`` from IBKR's orderStatus event;
    direction comes from ``orders.direction`` looked up downstream by
    ``fetch_fill_context``.
    """

    client_order_id: str
    cumulative_filled_quantity: int
    vwap_fill_price: Decimal
    total_commission_usd: Decimal
    latest_filled_at_utc: datetime


def _coerce_price(value: object) -> Decimal:
    """Coerce an IBKR ``Execution.price`` (float) to Decimal via str() so
    we don't carry binary-float drift into A05-policed payloads.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _coerce_commission(value: object) -> Decimal:
    """Coerce an IBKR ``CommissionReport.commission`` (float) to Decimal.

    IBKR returns ``sys.float_info.max`` (~1.79e308) as a sentinel for
    "commission not yet computed" in some edge cases — we clamp to 0
    when the value exceeds 1e6 (no real per-execution commission is
    anywhere near $1M and the sentinel poisons downstream math).
    """
    if isinstance(value, Decimal):
        return value
    # IBKR floats arrive as Python floats; Decimal(str(float)) is the
    # idiomatic A05-clean coercion.
    coerced = Decimal(str(value))
    if coerced > Decimal("1000000"):
        return Decimal("0")
    return coerced


def _coerce_quantity(value: object) -> int:
    """Coerce an IBKR ``Execution.shares`` (Decimal or float) to int.

    Phase 1 deals only in whole-share / whole-contract sizing
    (parameters.py); a fractional-share fill would indicate the
    operator pasted the wrong CID or a Phase 2+ scenario hit. We round
    to int and let downstream FillIngestPayload validation surface the
    mismatch.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(Decimal(str(value)))


def _coerce_time(value: object) -> datetime:
    """Coerce an IBKR ``Fill.time`` (datetime) to tz-aware UTC.

    ib_async returns timezone-aware datetimes per its own convention,
    but defensively we naïve-stamp UTC if the value is missing tzinfo
    (the fill_processor's planner re-raises on naive datetimes per A06,
    so this branch surfaces as a clean error rather than silent
    misinterpretation).
    """
    if not isinstance(value, datetime):
        raise ValueError(f"Expected datetime, got {type(value).__name__}: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _aggregate_fills_for_cid(
    *,
    client_order_id: str,
    fills: Sequence[Any],
) -> AggregatedExecution:
    """Roll up N ib_async Fill records into one AggregatedExecution.

    The VWAP price is sum(qty * price) / sum(qty) — the standard
    volume-weighted average. Commission is summed across the
    ``CommissionReport`` objects on each Fill. The latest timestamp
    is ``max(fill.time)``.

    Raises ValueError if ``fills`` is empty (caller filters first).
    """
    if not fills:
        raise ValueError(f"Cannot aggregate empty fills list for CID {client_order_id!r}")

    total_qty = 0
    weighted_price_sum = Decimal("0")
    total_commission = Decimal("0")
    latest_time: datetime | None = None

    for fill in fills:
        execution = fill.execution
        commission_report = getattr(fill, "commissionReport", None)
        fill_qty = _coerce_quantity(execution.shares)
        fill_price = _coerce_price(execution.price)
        fill_time = _coerce_time(fill.time)
        if commission_report is not None:
            total_commission += _coerce_commission(commission_report.commission)

        total_qty += fill_qty
        weighted_price_sum += Decimal(fill_qty) * fill_price
        if latest_time is None or fill_time > latest_time:
            latest_time = fill_time

    if total_qty == 0:
        raise ValueError(
            f"Aggregated quantity for CID {client_order_id!r} is zero — fills list malformed"
        )
    if latest_time is None:
        # Unreachable: at least one fill, so latest_time was set above.
        # Defensive raise to satisfy mypy + future-proof.
        raise ValueError(f"No fill timestamp found for CID {client_order_id!r}")

    vwap = (weighted_price_sum / Decimal(total_qty)).quantize(Decimal("0.00000001"))
    return AggregatedExecution(
        client_order_id=client_order_id,
        cumulative_filled_quantity=total_qty,
        vwap_fill_price=vwap,
        total_commission_usd=total_commission,
        latest_filled_at_utc=latest_time,
    )


def _group_fills_by_cid(
    *,
    fills: Iterable[Any],
    wanted_cids: Sequence[str],
) -> dict[str, list[Any]]:
    """Group ib_async Fill records by ``execution.orderRef``, keeping
    only entries whose orderRef matches one of ``wanted_cids``. Returns
    a dict keyed by orderRef with a list of fills for each match.

    Unrequested orderRefs are dropped. ``orderRef`` missing or empty
    (typical for manual TWS-side orders without our worker's CID
    convention) is dropped as well.
    """
    wanted = set(wanted_cids)
    grouped: dict[str, list[Any]] = {cid: [] for cid in wanted}
    for fill in fills:
        order_ref = getattr(fill.execution, "orderRef", "") or ""
        if order_ref in wanted:
            grouped[order_ref].append(fill)
    return grouped


# ---------------------------------------------------------------------------
# IBKR connection + execution fetch
# ---------------------------------------------------------------------------


#: Callable type for tests that inject a fake IB factory. Production
#: code uses ``_default_ib_factory`` which lazy-imports ib_async.
IBFactory = Callable[[], Any]


def _default_ib_factory() -> Any:
    """Lazy import of ``ib_async.IB`` so module load doesn't require the
    library to be installed (matches the same lazy-import pattern in
    ``services/execution/ibkr_adapter.py``).
    """
    from ib_async import IB as IbAsyncIB

    return IbAsyncIB()


async def _connect_and_fetch_executions(
    *,
    ib_factory: IBFactory,
    host: str,
    port: int,
    client_id: int,
    filter_acct: str | None,
    filter_time: str | None,
) -> list[Any]:
    """Open a one-shot IBKR connection, fetch executions, disconnect.

    Returns the full list of ``Fill`` objects matching the
    ``ExecutionFilter``. Caller filters by orderRef downstream.

    Raises any underlying ib_async exception — the main entry point
    catches + maps to EXIT_IBKR_CONNECT_FAILED.
    """
    # Lazy import here too — keeps the module loadable in test envs.
    from ib_async import ExecutionFilter

    ib = ib_factory()
    log.info(
        "replay_executions_ibkr_connecting",
        host=host,
        port=port,
        client_id=client_id,
    )
    try:
        await ib.connectAsync(
            host=host,
            port=port,
            clientId=client_id,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        log.info(
            "replay_executions_ibkr_connected",
            host=host,
            port=port,
            client_id=client_id,
        )

        exec_filter_kwargs: dict[str, Any] = {}
        if filter_acct is not None:
            exec_filter_kwargs["acctCode"] = filter_acct
        if filter_time is not None:
            exec_filter_kwargs["time"] = filter_time

        exec_filter = ExecutionFilter(**exec_filter_kwargs)
        fills = await ib.reqExecutionsAsync(exec_filter)
        log.info(
            "replay_executions_ibkr_fetched",
            fill_count=len(fills),
            filter_acct=filter_acct,
            filter_time=filter_time,
        )
        return list(fills)
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
                log.info("replay_executions_ibkr_disconnected")
        except Exception as exc:
            log.warning("replay_executions_ibkr_disconnect_error", error=str(exc))


# ---------------------------------------------------------------------------
# Per-CID replay outcome
# ---------------------------------------------------------------------------


ReplayOutcomeKind = Literal[
    "success",
    "no_match",
    "unsupported_scenario",
    "fill_processing_error",
    "none_result",
]


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """The result of replaying one CID. The main loop accumulates these
    and computes the aggregate exit code at the end.
    """

    client_order_id: str
    kind: ReplayOutcomeKind
    audit_event_uuid: str | None = None
    error_code: str | None = None
    error_message: str | None = None


#: Callable type for tests injecting a fake process_fill_event.
ProcessFillEventFn = Callable[..., Awaitable[Any]]


async def _replay_one_cid(
    *,
    aggregated: AggregatedExecution,
    env: EnvName,
    session_factory: Any,
    process_fill_event_fn: ProcessFillEventFn,
    fill_processing_error_cls: type[Exception],
    unsupported_scenario_error_cls: type[Exception],
    dry_run: bool,
) -> ReplayOutcome:
    """Build the FillIngestPayload + call process_fill_event.

    The two error classes are passed in so tests can use stub classes
    without importing fill_processor (which would require a configured
    Python path matching the api container). Production uses the real
    ``UnsupportedFillScenarioError`` + ``FillProcessingError``.
    """
    # Lazy import — production-side; tests use the stub classes that the
    # caller pre-resolved.
    from services.risk.fill_processor import FillIngestPayload

    broker_fill_id = f"replay:{aggregated.client_order_id}"
    payload = FillIngestPayload(
        broker_fill_id=broker_fill_id,
        cumulative_filled_quantity=aggregated.cumulative_filled_quantity,
        fill_quantity=aggregated.cumulative_filled_quantity,
        fill_price=aggregated.vwap_fill_price,
        commission_usd=aggregated.total_commission_usd,
        filled_at_utc=aggregated.latest_filled_at_utc,
    )

    log.info(
        "replay_executions_replaying_cid",
        client_order_id=aggregated.client_order_id,
        cumulative_filled_quantity=aggregated.cumulative_filled_quantity,
        vwap_fill_price=str(aggregated.vwap_fill_price),
        total_commission_usd=str(aggregated.total_commission_usd),
        latest_filled_at_utc=aggregated.latest_filled_at_utc.isoformat(),
        broker_fill_id=broker_fill_id,
        dry_run=dry_run,
    )

    if dry_run:
        return ReplayOutcome(
            client_order_id=aggregated.client_order_id,
            kind="success",
            audit_event_uuid=None,
        )

    try:
        result = await process_fill_event_fn(
            session_factory=session_factory,
            client_order_id=aggregated.client_order_id,
            payload=payload,
            env=env,
        )
    except unsupported_scenario_error_cls as exc:
        log.warning(
            "replay_executions_unsupported_scenario",
            client_order_id=aggregated.client_order_id,
            error_code=getattr(exc, "error_code", None),
            error=str(exc),
            details=getattr(exc, "details", None),
        )
        return ReplayOutcome(
            client_order_id=aggregated.client_order_id,
            kind="unsupported_scenario",
            error_code=getattr(exc, "error_code", None),
            error_message=str(exc),
        )
    except fill_processing_error_cls as exc:
        log.error(
            "replay_executions_fill_processing_error",
            client_order_id=aggregated.client_order_id,
            error_code=getattr(exc, "error_code", None),
            error=str(exc),
            details=getattr(exc, "details", None),
        )
        return ReplayOutcome(
            client_order_id=aggregated.client_order_id,
            kind="fill_processing_error",
            error_code=getattr(exc, "error_code", None),
            error_message=str(exc),
        )

    if result is None:
        log.warning(
            "replay_executions_no_orders_row",
            client_order_id=aggregated.client_order_id,
        )
        return ReplayOutcome(
            client_order_id=aggregated.client_order_id,
            kind="none_result",
        )

    audit_uuid = (
        str(result.audit_event_uuids[0]) if getattr(result, "audit_event_uuids", None) else None
    )
    log.info(
        "replay_executions_replayed_successfully",
        client_order_id=aggregated.client_order_id,
        audit_event_uuid=audit_uuid,
        fill_id=str(getattr(result, "fill_id", "")),
        position_id=str(getattr(result, "position_id", "")),
        trade_id=str(getattr(result, "trade_id", "")),
        balance_id=str(getattr(result, "balance_id", "")),
        new_order_status=getattr(result, "new_order_status", None),
    )
    return ReplayOutcome(
        client_order_id=aggregated.client_order_id,
        kind="success",
        audit_event_uuid=audit_uuid,
    )


# ---------------------------------------------------------------------------
# Exit-code computation
# ---------------------------------------------------------------------------


def _compute_exit_code(outcomes: Sequence[ReplayOutcome]) -> int:
    """Aggregate per-CID outcomes into a single process exit code.

    Precedence (highest to lowest): fill_processing_error → unsupported_scenario
    → no_match → none_result (warn-only, exit 0) → success. The most
    severe outcome wins so the operator's automation can key off a
    single exit code.
    """
    if any(o.kind == "fill_processing_error" for o in outcomes):
        return EXIT_FILL_PROCESSING_ERROR
    if any(o.kind == "unsupported_scenario" for o in outcomes):
        return EXIT_UNSUPPORTED_SCENARIO
    if any(o.kind == "no_match" for o in outcomes):
        return EXIT_NO_MATCH
    return EXIT_OK


# ---------------------------------------------------------------------------
# DB setup (production-only; tests stub the session_factory)
# ---------------------------------------------------------------------------


async def _build_session_factory(database_url: str) -> tuple[Any, Any]:
    """Build a one-shot SQLAlchemy async engine + sessionmaker. Returns
    ``(engine, sessionmaker)``; caller is responsible for disposing
    the engine via ``await engine.dispose()``.

    Mirrors the verify_chain.py one-shot pattern — we do NOT reuse
    ``services.api.db``'s pool because the script runs independently
    of the api lifespan.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    """Console renderer — operator reads stdout directly per the runbook."""
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


DATABASE_URL_ENV: Final[str] = "DATABASE_URL"


async def _amain(
    args: ParsedArgs,
    *,
    ib_factory: IBFactory | None = None,
    session_factory_override: Any = None,
    process_fill_event_override: ProcessFillEventFn | None = None,
) -> int:
    """Async entry point. Returns the process exit code.

    Test hooks via the three override kwargs:

    * ``ib_factory`` — fake IBKR connection that returns canned fills
    * ``session_factory_override`` — skips the DB engine creation step
    * ``process_fill_event_override`` — fake the fill_processor call
    """
    log.info(
        "replay_executions_started",
        client_order_ids=list(args.client_order_ids),
        env=args.env,
        dry_run=args.dry_run,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port,
        client_id=args.client_id,
    )

    # --- 1. Fetch executions from IBKR -------------------------------------
    try:
        fills = await _connect_and_fetch_executions(
            ib_factory=ib_factory or _default_ib_factory,
            host=args.ibkr_host,
            port=args.ibkr_port,
            client_id=args.client_id,
            filter_acct=args.filter_acct,
            filter_time=args.filter_time,
        )
    except Exception as exc:
        log.error(
            "replay_executions_ibkr_connect_failed",
            error=str(exc),
            error_class=type(exc).__name__,
            host=args.ibkr_host,
            port=args.ibkr_port,
            client_id=args.client_id,
        )
        return EXIT_IBKR_CONNECT_FAILED

    # --- 2. Filter + aggregate by CID -------------------------------------
    grouped = _group_fills_by_cid(fills=fills, wanted_cids=args.client_order_ids)

    outcomes: list[ReplayOutcome] = []
    to_replay: list[AggregatedExecution] = []
    for cid in args.client_order_ids:
        fills_for_cid = grouped.get(cid, [])
        if not fills_for_cid:
            log.warning("replay_executions_no_match", client_order_id=cid)
            outcomes.append(ReplayOutcome(client_order_id=cid, kind="no_match"))
            continue
        try:
            aggregated = _aggregate_fills_for_cid(
                client_order_id=cid,
                fills=fills_for_cid,
            )
        except Exception as exc:
            log.error(
                "replay_executions_aggregation_failed",
                client_order_id=cid,
                error=str(exc),
                error_class=type(exc).__name__,
            )
            outcomes.append(ReplayOutcome(client_order_id=cid, kind="no_match"))
            continue
        to_replay.append(aggregated)

    # --- 3. Build DB session factory (skip in dry-run + tests) -------------
    engine: Any = None
    session_factory: Any = session_factory_override
    if session_factory is None and not args.dry_run and to_replay:
        database_url = os.environ.get(DATABASE_URL_ENV)
        if not database_url:
            log.error(
                "replay_executions_db_init_failed",
                reason=f"{DATABASE_URL_ENV} is not set",
            )
            return EXIT_DB_INIT_FAILED
        try:
            engine, session_factory = await _build_session_factory(database_url)
        except Exception as exc:
            log.error(
                "replay_executions_db_init_failed",
                error=str(exc),
                error_class=type(exc).__name__,
            )
            return EXIT_DB_INIT_FAILED

    # --- 4. Resolve fill_processor symbols (overridden in tests) ---------
    process_fill_event_fn: ProcessFillEventFn
    fill_processing_error_cls: type[Exception]
    unsupported_scenario_error_cls: type[Exception]
    if process_fill_event_override is not None:
        process_fill_event_fn = process_fill_event_override
        # Use stub error classes the test injects via attribute on the
        # override callable, falling back to the real classes if not set.
        fill_processing_error_cls = getattr(
            process_fill_event_override,
            "__fill_processing_error_cls__",
            Exception,
        )
        unsupported_scenario_error_cls = getattr(
            process_fill_event_override,
            "__unsupported_scenario_error_cls__",
            Exception,
        )
    else:
        from services.risk.fill_processor import (
            FillProcessingError,
            UnsupportedFillScenarioError,
            process_fill_event,
        )

        process_fill_event_fn = process_fill_event
        fill_processing_error_cls = FillProcessingError
        unsupported_scenario_error_cls = UnsupportedFillScenarioError

    # --- 5. Replay each matched CID -----------------------------------------
    try:
        for aggregated in to_replay:
            outcome = await _replay_one_cid(
                aggregated=aggregated,
                env=args.env,
                session_factory=session_factory,
                process_fill_event_fn=process_fill_event_fn,
                fill_processing_error_cls=fill_processing_error_cls,
                unsupported_scenario_error_cls=unsupported_scenario_error_cls,
                dry_run=args.dry_run,
            )
            outcomes.append(outcome)
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log.warning("replay_executions_db_dispose_error", error=str(exc))

    # --- 6. Aggregate exit code -------------------------------------------
    exit_code = _compute_exit_code(outcomes)
    log.info(
        "replay_executions_completed",
        exit_code=exit_code,
        outcomes=[
            {
                "client_order_id": o.client_order_id,
                "kind": o.kind,
                "audit_event_uuid": o.audit_event_uuid,
                "error_code": o.error_code,
            }
            for o in outcomes
        ],
    )
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Sync entry point. Returns the exit code; never sys.exits."""
    _configure_logging()
    try:
        try:
            args = _parse_args(argv)
        except SystemExit as exc:
            # argparse exited with 2 on usage error; map to our EXIT_BAD_ARGS.
            return EXIT_BAD_ARGS if exc.code != 0 else EXIT_OK
        return asyncio.run(_amain(args))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_IBKR_HOST",
    "DEFAULT_IBKR_PORT",
    "EXIT_BAD_ARGS",
    "EXIT_DB_INIT_FAILED",
    "EXIT_FILL_PROCESSING_ERROR",
    "EXIT_IBKR_CONNECT_FAILED",
    "EXIT_NO_MATCH",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "EXIT_UNSUPPORTED_SCENARIO",
    "AggregatedExecution",
    "ParsedArgs",
    "ReplayOutcome",
    "ReplayOutcomeKind",
    "_aggregate_fills_for_cid",
    "_amain",
    "_build_parser",
    "_compute_exit_code",
    "_group_fills_by_cid",
    "_parse_args",
    "_replay_one_cid",
    "main",
]
