"""scripts/operator_tools/master_client_id_probe.py — empirical
cross-clientId cancellation probe.

Operator-coordinated validation step for Mitigation A of the cross-
clientId IBKR cancellation limitation (see ``Docs/decisions-log.md``
2026-05-27 entry). Runs after the gateway has been restarted with
``TWS_MASTER_CLIENT_ID`` set in ``deploy/.env`` + docker-compose
re-recreated, confirming empirically that:

    1. A clientId other than the master CAN place an order.
    2. The master clientId CAN cancel that order — pre-mitigation,
       this raised ``Error 10147 "OrderId not found"``; post-
       mitigation, the cancel transitions the trade to ``Cancelled``
       within the PR #262 2-second verification window.

If step 2 fails post-deploy, the master designation didn't take — most
likely ``TWS_MASTER_CLIENT_ID`` empty-string passed through to IBC as
a no-op; recheck the rendered config.ini per the decisions-log entry.

**Safety design.**

* The placed order is a ``stop_market`` SELL on the supplied market
  with ``stop_price=1.0`` — well below any realistic /MES price, so
  the stop will NEVER trigger. The order sits as ``PreSubmitted`` at
  IBKR until cancelled.
* On any failure path (including a successful place but failed cancel
  from the master), a final cleanup pass via ``reqGlobalCancel`` from
  a third clientId fires as belt-and-suspenders — ensuring no probe
  order survives past the script's exit.
* Day TIF: even if cleanup also fails, the order expires at the next
  daily settlement (~21:00 UTC for /MES). The operator should still
  verify cleanup in TWS, but the system-side fallback is the daily
  expiry.

**Worker-collision gate (2026-05-28 refactor).**

The probe's Stage 2 connects to ``ib_gateway`` as ``--master-client-id``.
In the production allocation that clientId IS the long-lived order worker
(``api`` container, ``clientId=1`` per dev-guide §1.5 LOCKED — or ``2``
under the VPS ``API_IBKR_CLIENT_ID`` override). IBKR refuses a second
connection on a clientId that's already in use; if the worker is up, the
Stage 2 connect fails with ``Error 162`` and wedges the colliding client
for ~30 min.

To avoid this, the operator MUST stop the api worker before running the
probe. The ``--worker-offline-confirmed`` flag is the explicit
acknowledgment — without it the probe refuses to run when
``--master-client-id`` is in the worker-allocation range {1, 2} per
dev-guide §1.5. The gate exits with ``EXIT_BAD_ARGS=6`` and the operator
runbook in the error message.

**Operator runbook — worker-offline probe ceremony.**

Run during the CME 02:00-04:00 UTC trough so the worker outage is
invisible to any scheduled cycle::

    # 1. Stop the worker (clientId=1 frees up)
    ssh root@<vps> 'docker compose --env-file deploy/.env stop api'

    # 2. Wait for clean disconnect (~5 s)
    sleep 5

    # 3. Run the probe
    python -m scripts.operator_tools.master_client_id_probe \\
        --master-client-id 1 \\
        --env paper \\
        --worker-offline-confirmed

    # 4. Restart the worker (it reclaims clientId=1)
    ssh root@<vps> 'docker compose --env-file deploy/.env start api'

    # 5. Verify worker reconnects + heartbeats resume
    ssh root@<vps> 'docker logs api --since 2m 2>&1 | grep -E "ibkr_connected|bar_sync_worker_spawned"'

Empirical results from each run should be appended to
``Docs/decisions-log.md`` 2026-05-27 entry "Cross-clientId IBKR
cancellation: structural limitation, mitigation = Master Client ID"
in the operator-decisions deferred subsection.

**Forbidden-paths check.** ``scripts/operator_tools/**`` is NOT on the
dev-guide §11 anti-pattern [A02] forbidden-modification whitelist nor
on the §2.3 hot-fix whitelist. Pure tooling — no ``services/risk/**``
or ``services/execution/**`` modifications. The script CALLS those
modules (via ``IbAsyncIbkrClient``) but does not modify them. Regular
PR review applies.

**A-gates:**

* **A01 N/A** — no audit writes; the probe is read-only-against-state
  with a single transient broker round-trip.
* **A05 enforced** — quantity + stop_price are ``Decimal`` at the
  IBKR boundary.
* **A06 enforced** — every datetime tz-aware UTC where logged.
* **A22 N/A** — testcontainers not needed; the script doesn't touch
  the DB. Unit tests cover the structured-result envelope shape +
  arg parsing.

Exit codes (load-bearing for the operator-runbook contract):

* ``0`` — success: place via probe clientId succeeded, cancel via
  master clientId succeeded, cleanup global-cancel succeeded
* ``1`` — placement on probe clientId failed (gateway unreachable,
  wedged clientId, etc.) — operator investigates gateway state
* ``2`` — master clientId cancel failed (most likely the mitigation
  didn't take — see decisions-log entry for diagnostics)
* ``3`` — cleanup global-cancel failed — operator manually clears
  via TWS or via a separate ``reqGlobalCancel`` invocation
* ``4`` — IBKR connection failure on any of the three clientIds
* ``6`` — Invalid CLI args (caught early, before any I/O)
* ``99`` — Unexpected exception (logged with traceback; operator
  escalates)

Usage::

    python -m scripts.operator_tools.master_client_id_probe \\
        --master-client-id 1 \\
        --env paper

For the canonical end-to-end ceremony including the gateway restart
prerequisite, see ``Docs/decisions-log.md`` 2026-05-27 entry
"Cross-clientId IBKR cancellation: structural limitation, mitigation =
Master Client ID" — "Deploy sequence" subsection.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal
from uuid import uuid4

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit code contract (locked; documented in module docstring)
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_PLACE_FAILED: Final[int] = 1
EXIT_MASTER_CANCEL_FAILED: Final[int] = 2
EXIT_CLEANUP_FAILED: Final[int] = 3
EXIT_IBKR_CONNECT_FAILED: Final[int] = 4
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# Defaults (locked clientId conventions per dev-guide §1.5)
# ---------------------------------------------------------------------------

#: Env values accepted on the CLI. Mirrors ``audit_log.env`` CHECK
#: constraint even though this script does no audit writes — keeps the
#: operator-tool surface consistent across all probes.
_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

#: Probe clientIds. 86 = placing client (non-master); 87 = cleanup
#: client (non-master). Both must be in the 80-99 operator-tools range
#: per dev-guide §1.5 LOCKED so they don't collide with the worker
#: (1-2), bar_sync (3), or reserved lanes (4-7).
DEFAULT_PLACE_CLIENT_ID: Final[int] = 86
DEFAULT_CLEANUP_CLIENT_ID: Final[int] = 87

#: Default market for the probe. /MES is liquid + cheap (1 contract
#: ≈ $5/point margin on the paper side) so a probe order has minimal
#: capital impact even if cleanup fails + the order somehow fills
#: (impossible at stop=1.0 but stated for transparency).
DEFAULT_MARKET: Final[str] = "/MES"

#: Stop price for the probe order. /MES never trades at $1; the stop
#: will NEVER trigger. If /MES does reach $1, the entire market has
#: deeper problems than this probe.
SAFE_STOP_PRICE: Final[Decimal] = Decimal("1")

#: Default ib_gateway host / port — matches docker-compose service +
#: paper-account port. Override via CLI when probing live-* envs.
DEFAULT_IBKR_HOST: Final[str] = "ib_gateway"
DEFAULT_IBKR_PORT: Final[int] = 4004

#: Per-step connect timeout. Each of the three clientId connects gets
#: its own budget; total wall time is bounded at 3x this value.
CONNECT_TIMEOUT_SECONDS: Final[float] = 30.0

#: Per-call broker round-trip timeout. Generous because the
#: cross-clientId cancel may hit the PR #262 2-second verification
#: window plus IBKR's own ack latency.
BROKER_CALL_TIMEOUT_SECONDS: Final[float] = 15.0

#: ClientIds reserved for the long-lived api order worker per dev-guide
#: §1.5 LOCKED — ``1`` is the code default + ``2`` is the VPS deploy
#: override. The probe's Stage 2 connects as the master clientId; if the
#: worker holds it concurrently IBKR returns Error 162 + wedges the
#: caller for ~30 min. ``--worker-offline-confirmed`` is the operator's
#: explicit acknowledgment that the api container is stopped for the
#: probe window. See module docstring "Worker-collision gate" subsection.
_WORKER_CLIENT_ID_RANGE: Final[frozenset[int]] = frozenset({1, 2})


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests use this surface directly without
    going through argparse so they don't have to fight stderr capture.
    """

    master_client_id: int
    place_client_id: int
    cleanup_client_id: int
    market: str
    env: EnvName
    ibkr_host: str
    ibkr_port: int
    allow_non_paper: bool
    worker_offline_confirmed: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.master_client_id_probe",
        description=(
            "Empirical cross-clientId cancellation probe. Runs after "
            "ib_gateway has been restarted with TWS_MASTER_CLIENT_ID "
            "configured per Docs/decisions-log.md 2026-05-27 entry."
        ),
    )
    parser.add_argument(
        "--master-client-id",
        type=int,
        required=True,
        help=(
            "The clientId designated as Master in the IBKR gateway "
            "(matches the value of IBKR_MASTER_CLIENT_ID in deploy/.env). "
            "Typically the worker's clientId — 1 (code default) or 2 "
            "(VPS deploy override)."
        ),
    )
    parser.add_argument(
        "--place-client-id",
        type=int,
        default=DEFAULT_PLACE_CLIENT_ID,
        help=(
            f"Operator-tool clientId for placing the probe order "
            f"(default: {DEFAULT_PLACE_CLIENT_ID}). Must be in 80-99 "
            "per dev-guide §1.5 LOCKED + must differ from "
            "--master-client-id."
        ),
    )
    parser.add_argument(
        "--cleanup-client-id",
        type=int,
        default=DEFAULT_CLEANUP_CLIENT_ID,
        help=(
            f"Operator-tool clientId for the final reqGlobalCancel "
            f"cleanup pass (default: {DEFAULT_CLEANUP_CLIENT_ID}). Must "
            "be in 80-99 per dev-guide §1.5 LOCKED + must differ from "
            "both --master-client-id and --place-client-id."
        ),
    )
    parser.add_argument(
        "--market",
        default=DEFAULT_MARKET,
        help=(
            f"Market for the probe order (default: {DEFAULT_MARKET!r}). "
            "Must be in the Phase 1 universe; /MES recommended for "
            "minimum margin impact."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Audit env tag. The probe itself does not write to "
            "audit_log; this flag mirrors the other operator-tool CLIs "
            "for consistency + flips the --allow-non-paper safety gate "
            "on live-*."
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
        help=f"ib_gateway port (default: {DEFAULT_IBKR_PORT}; 4004=paper, 4003=live).",
    )
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        help=(
            "Required to run with --env in {live-small,live-scale}. "
            "The probe places a real order at the broker; on paper "
            "this is harmless (stop=$1 + Day TIF), on live it still "
            "consumes margin until cancellation. Operator must opt in "
            "explicitly to acknowledge."
        ),
    )
    parser.add_argument(
        "--worker-offline-confirmed",
        action="store_true",
        help=(
            "Required when --master-client-id is in the worker-allocation "
            "range {1, 2} per dev-guide §1.5. Acknowledges that the api "
            "container is currently stopped — without this, the probe's "
            "Stage 2 connect would collide with the running worker and "
            "wedge clientId at IBKR for ~30 min. See module docstring "
            "'Operator runbook — worker-offline probe ceremony' for the "
            "full stop/run/start sequence."
        ),
    )
    return parser


def parse_args(argv: list[str]) -> ParsedArgs:
    parsed = _build_parser().parse_args(argv)
    master_cid = int(parsed.master_client_id)
    place_cid = int(parsed.place_client_id)
    cleanup_cid = int(parsed.cleanup_client_id)
    if place_cid == master_cid:
        raise ValueError("--place-client-id must differ from --master-client-id")
    if cleanup_cid == master_cid:
        raise ValueError("--cleanup-client-id must differ from --master-client-id")
    if cleanup_cid == place_cid:
        raise ValueError("--cleanup-client-id must differ from --place-client-id")
    if not (80 <= place_cid <= 99):
        raise ValueError(f"--place-client-id={place_cid} must be in 80-99 per dev-guide §1.5")
    if not (80 <= cleanup_cid <= 99):
        raise ValueError(f"--cleanup-client-id={cleanup_cid} must be in 80-99 per dev-guide §1.5")
    if parsed.env != "paper" and not parsed.allow_non_paper:
        raise ValueError(
            f"--env={parsed.env} requires --allow-non-paper to acknowledge "
            "that a real order will hit the broker"
        )
    if master_cid in _WORKER_CLIENT_ID_RANGE and not parsed.worker_offline_confirmed:
        raise ValueError(
            f"--master-client-id={master_cid} is in the worker-allocation range "
            f"{{1, 2}} per dev-guide §1.5; the api order worker holds this "
            "clientId in normal operation and IBKR refuses a concurrent "
            "second connection on it. Stop the worker first and pass "
            "--worker-offline-confirmed:\n"
            "  ssh root@<vps> 'docker compose --env-file deploy/.env stop api'\n"
            "  python -m scripts.operator_tools.master_client_id_probe \\\n"
            f"      --master-client-id {master_cid} --env {parsed.env} "
            "--worker-offline-confirmed\n"
            "  ssh root@<vps> 'docker compose --env-file deploy/.env start api'\n"
            "Run during the CME 02:00-04:00 UTC trough so the worker outage "
            "is invisible to scheduled cycles."
        )
    return ParsedArgs(
        master_client_id=master_cid,
        place_client_id=place_cid,
        cleanup_client_id=cleanup_cid,
        market=str(parsed.market),
        env=parsed.env,
        ibkr_host=str(parsed.ibkr_host),
        ibkr_port=int(parsed.ibkr_port),
        allow_non_paper=bool(parsed.allow_non_paper),
        worker_offline_confirmed=bool(parsed.worker_offline_confirmed),
    )


def _build_probe_client_order_id() -> str:
    """Construct a 33-char client_order_id per backend-spec §2.5.

    Format: ``mcidprob-mcidprob-<hex16>-0`` — opaque-but-greppable
    prefix so the operator can identify probe orders in TWS or in
    audit-log forensics, and matches the locked format constraint
    (33 chars, 4 hyphen-separated segments, last segment numeric).
    """
    return f"mcidprob-mcidprob-{uuid4().hex[:16]}-0"


async def _run_probe(args: ParsedArgs) -> int:
    """Three-stage cross-clientId cancellation probe.

    Each stage opens its own ib-async connection on a distinct
    clientId; the connection closes before the next stage begins so
    IBKR sees three sequential sessions, not three concurrent sockets
    (matches the dev-guide §1.5 LOCKED clientId allocation model).
    """
    from services.execution.ibkr_adapter import IbAsyncIbkrClient
    from services.execution.types import (
        IbkrPlacementError,
        IbkrPlaceOrderRequest,
    )

    probe_cid = _build_probe_client_order_id()
    log_bound = log.bind(
        probe_client_order_id=probe_cid,
        master_client_id=args.master_client_id,
        place_client_id=args.place_client_id,
        cleanup_client_id=args.cleanup_client_id,
        market=args.market,
        env=args.env,
        started_at_utc=datetime.now(tz=UTC).isoformat(),
    )
    log_bound.info("master_client_id_probe_starting")

    # ----- Stage 1: place probe order on non-master clientId -----
    place_client = IbAsyncIbkrClient(
        host=args.ibkr_host,
        port=args.ibkr_port,
        account_id=None,
        client_id=args.place_client_id,
    )
    try:
        try:
            await asyncio.wait_for(place_client.connect(), timeout=CONNECT_TIMEOUT_SECONDS)
        except (IbkrPlacementError, TimeoutError) as exc:
            log_bound.error(
                "master_client_id_probe_place_connect_failed",
                error=str(exc),
            )
            return EXIT_IBKR_CONNECT_FAILED

        try:
            contract = await asyncio.wait_for(
                place_client.resolve_contract(args.market),
                timeout=BROKER_CALL_TIMEOUT_SECONDS,
            )
        except (IbkrPlacementError, TimeoutError) as exc:
            log_bound.error(
                "master_client_id_probe_resolve_contract_failed",
                error=str(exc),
            )
            return EXIT_PLACE_FAILED

        place_request = IbkrPlaceOrderRequest(
            client_order_id=probe_cid,
            contract=contract,
            side="sell",
            quantity=Decimal("1"),
            order_type="stop_market",
            stop_price=SAFE_STOP_PRICE,
            time_in_force="DAY",
            transmit=True,
        )
        try:
            place_result = await asyncio.wait_for(
                place_client.place_order(place_request),
                timeout=BROKER_CALL_TIMEOUT_SECONDS,
            )
        except (IbkrPlacementError, TimeoutError) as exc:
            log_bound.error(
                "master_client_id_probe_place_failed",
                error=str(exc),
            )
            return EXIT_PLACE_FAILED
        log_bound = log_bound.bind(
            place_broker_order_id=place_result.broker_order_id,
            place_status=place_result.status,
        )
        log_bound.info("master_client_id_probe_placed")
    finally:
        try:
            await place_client.disconnect()
        except Exception as exc:
            log_bound.warning(
                "master_client_id_probe_place_disconnect_failed",
                error=str(exc),
            )

    # ----- Stage 2: cancel via master clientId (the actual test) -----
    master_client = IbAsyncIbkrClient(
        host=args.ibkr_host,
        port=args.ibkr_port,
        account_id=None,
        client_id=args.master_client_id,
    )
    master_cancel_succeeded = False
    try:
        try:
            await asyncio.wait_for(master_client.connect(), timeout=CONNECT_TIMEOUT_SECONDS)
        except (IbkrPlacementError, TimeoutError) as exc:
            log_bound.error(
                "master_client_id_probe_master_connect_failed",
                error=str(exc),
            )
            return EXIT_IBKR_CONNECT_FAILED

        try:
            cancel_result = await asyncio.wait_for(
                master_client.cancel_order(probe_cid),
                timeout=BROKER_CALL_TIMEOUT_SECONDS,
            )
            master_cancel_succeeded = True
            log_bound = log_bound.bind(
                cancel_broker_order_id=cancel_result.broker_order_id,
                cancel_submitted_at_utc=cancel_result.cancel_submitted_at_utc.isoformat(),
            )
            log_bound.info(
                "master_client_id_probe_master_cancel_succeeded",
                note=(
                    "Cross-clientId cancellation works — master designation "
                    "is active at the gateway."
                ),
            )
        except IbkrPlacementError as exc:
            log_bound.error(
                "master_client_id_probe_master_cancel_failed",
                error=str(exc),
                note=(
                    "Master clientId could not cancel the probe order. "
                    "Most likely TWS_MASTER_CLIENT_ID didn't render into "
                    "the gateway's config.ini — see Docs/decisions-log.md "
                    "2026-05-27 entry diagnostics subsection."
                ),
            )
        except TimeoutError:
            log_bound.error(
                "master_client_id_probe_master_cancel_timeout",
                timeout_s=BROKER_CALL_TIMEOUT_SECONDS,
            )
    finally:
        try:
            await master_client.disconnect()
        except Exception as exc:
            log_bound.warning(
                "master_client_id_probe_master_disconnect_failed",
                error=str(exc),
            )

    # ----- Stage 3: belt-and-suspenders reqGlobalCancel cleanup -----
    cleanup_client = IbAsyncIbkrClient(
        host=args.ibkr_host,
        port=args.ibkr_port,
        account_id=None,
        client_id=args.cleanup_client_id,
    )
    cleanup_cancelled_count: int | None = None
    try:
        try:
            await asyncio.wait_for(cleanup_client.connect(), timeout=CONNECT_TIMEOUT_SECONDS)
        except (IbkrPlacementError, TimeoutError) as exc:
            log_bound.error(
                "master_client_id_probe_cleanup_connect_failed",
                error=str(exc),
            )
            return EXIT_CLEANUP_FAILED

        try:
            cleanup_cancelled_count = await asyncio.wait_for(
                cleanup_client.cancel_all_orders(),
                timeout=BROKER_CALL_TIMEOUT_SECONDS,
            )
            log_bound = log_bound.bind(
                cleanup_cancelled_count=cleanup_cancelled_count,
            )
            log_bound.info(
                "master_client_id_probe_cleanup_completed",
                note=(
                    "reqGlobalCancel issued from cleanup clientId; any "
                    "residual probe order has been belt-and-suspenders "
                    "cleared."
                ),
            )
        except (IbkrPlacementError, TimeoutError) as exc:
            log_bound.error(
                "master_client_id_probe_cleanup_failed",
                error=str(exc),
                note=(
                    "Cleanup reqGlobalCancel failed — operator MUST "
                    "verify in TWS that no probe order survives. The "
                    "Day TIF ensures eventual expiry but operator "
                    "should not rely on it."
                ),
            )
            return EXIT_CLEANUP_FAILED
    finally:
        try:
            await cleanup_client.disconnect()
        except Exception as exc:
            log_bound.warning(
                "master_client_id_probe_cleanup_disconnect_failed",
                error=str(exc),
            )

    if not master_cancel_succeeded:
        return EXIT_MASTER_CANCEL_FAILED
    return EXIT_OK


async def _amain(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except (ValueError, SystemExit) as exc:
        # argparse raises SystemExit on --help / bad args; we
        # short-circuit to EXIT_BAD_ARGS for the runbook contract.
        if isinstance(exc, SystemExit):
            return exc.code if isinstance(exc.code, int) else EXIT_BAD_ARGS
        log.error("master_client_id_probe_bad_args", error=str(exc))
        return EXIT_BAD_ARGS

    try:
        return await _run_probe(args)
    except Exception as exc:  # pragma: no cover - terminal safety net
        log.error(
            "master_client_id_probe_unexpected",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return EXIT_UNEXPECTED


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_CLEANUP_CLIENT_ID",
    "DEFAULT_MARKET",
    "DEFAULT_PLACE_CLIENT_ID",
    "EXIT_BAD_ARGS",
    "EXIT_CLEANUP_FAILED",
    "EXIT_IBKR_CONNECT_FAILED",
    "EXIT_MASTER_CANCEL_FAILED",
    "EXIT_OK",
    "EXIT_PLACE_FAILED",
    "EXIT_UNEXPECTED",
    "SAFE_STOP_PRICE",
    "ParsedArgs",
    "main",
    "parse_args",
]
