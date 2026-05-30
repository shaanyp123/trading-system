"""scripts/operator_tools/recon_positions_probe.py — read-only EOD-recon
positions diagnostic (cache-timing race vs phantom positions).

**Why this exists.** The 2026-05-29 22:30 UTC EOD reconciliation cycle halted
the kill switch (``NORMAL → HALT_NEW``, reason ``recon_mismatch``) on two
``position_qty`` breaks (``/M2K`` + ``/MES``, ``expected=1 / actual=0``,
``source=tws_api``). The Option C reqPositions cutover did NOT fix the recurring
nightly false halt — it swapped FlexQuery's T+1 same-day blindness for a
*reqPositions cache-timing race*:

    services/reconciliation/ibkr_intraday.py::fetch_recon_positions
      → services/execution/ibkr_adapter.py::get_positions
        → ib.positions(account=...)        # ib-async LOCAL cache

``ib.positions()`` reads ib-async's local position cache. ``connectAsync()``
kicks off the ``reqPositions`` / ``positionEnd`` handshake that POPULATES that
cache, but does NOT await it — so a ``get_positions()`` issued immediately
after connect can read an EMPTY cache and return ``raw_count=0``. An
empty-but-successful return is not an exception, so #291's exception-only
FlexQuery fallback never fired; the planner treated "broker holds 0 futures"
as a valid broker view and tripped a false break per backend FUT market.

**What this probe answers — real or phantom?** It is the GATE before the
``services/reconciliation/**`` Path-B fix. It connects on a dedicated operator
clientId and, on ONE session, captures three views of the same account so the
operator can see the race directly:

  1. **cache @ connect** — ``ib.positions()`` read IMMEDIATELY after connect.
     This is exactly what the buggy recon path reads. Expected to be EMPTY or
     PARTIAL = the race reproduced.
  2. **fresh reqPositions** — ``await ib.reqPositionsAsync()`` (sends
     ``reqPositions``, AWAITS ``positionEnd``). This is the deterministic view
     and is what Path B will switch the recon fetch onto. Expected to list the
     REAL open positions.
  3. **cache @ settle** — ``ib.positions()`` re-read after a short settle
     delay, proving the cache populates given time.

It also lists open orders across ALL clients via ``reqAllOpenOrdersAsync`` so
the operator can confirm the protective stops placed by the order worker
(clientId 1/2) are live.

**Verdict.** If view (2) lists ``/M2K`` + ``/MES`` (or whatever
``--expect-markets`` names) → positions are REAL, the race is confirmed, and
Path B is the correct fix. If view (2) is ALSO empty → the positions are
PHANTOM (the backend believes it holds something the broker does not) and this
is a different problem (backend-vs-broker cleanup) that must be escalated, NOT
patched with Path B.

**Safety — purely read-only.** No order is placed; no ``reqGlobalCancel`` is
issued; nothing is written to the DB or audit chain. ``reqPositions`` /
``reqAllOpenOrders`` are account-wide read calls. The probe uses its OWN
clientId in the operator range {80-99} so it does NOT collide with the order
worker (1/2), bar_sync (3), or recon (4) — IBKR permits multiple distinct
clientIds on one gateway session (dev-guide §1.5 LOCKED), so this is safe to
run while the worker is up. Unlike ``master_client_id_probe.py`` there is NO
worker-offline requirement.

**Forbidden-paths check.** ``scripts/operator_tools/**`` is NOT on the
dev-guide §11 [A02] forbidden-modification whitelist nor the §2.3 hot-fix
whitelist. The probe constructs a raw ``ib_async.IB`` directly (rather than the
``services/execution`` adapter) precisely so it can demonstrate the
cache-vs-fresh difference the adapter's ``get_positions()`` hides — it does NOT
import or modify any forbidden module. Regular PR review applies; no
``risk-review-approved`` label.

**A-gates:**

* **A01 N/A** — no audit writes; read-only broker round-trips only.
* **A05 enforced** — quantity / avg-cost are ``Decimal`` at the IBKR boundary.
* **A06 enforced** — every datetime tz-aware UTC where logged.
* **A27** — talks to IBKR (third-party). The operator-run-against-live-gateway
  ceremony in the docstring below IS the smoke; there is no CI fixture for a
  real ``reqPositions`` cycle.

Exit codes (load-bearing for the operator-runbook contract):

* ``0`` — probe completed: connected, captured all three views + open orders.
  (Exit 0 does NOT mean "positions found" — read the verdict line; 0 means the
  probe ran cleanly.)
* ``4`` — IBKR connection failed (gateway unreachable / wedged clientId).
* ``5`` — a broker read call failed after a successful connect.
* ``6`` — invalid CLI args.
* ``99`` — unexpected exception (traceback on stderr).

Operator runbook — run inside the api container so ``ib_gateway`` resolves on
the Docker network (no DB / sops needed; the gateway needs no auth on the
internal network)::

    ssh root@178.156.239.84 -- 'set -euo pipefail
      cd /opt/trading
      docker compose --env-file deploy/.env exec -T \\
        -w /app -e PYTHONPATH=/app \\
        api \\
        /opt/venv/bin/python -m scripts.operator_tools.recon_positions_probe \\
          --env paper \\
          --expect-markets /M2K,/MES
    '

Empirical results should be appended to ``Docs/decisions-log.md`` 2026-05-29
under the Option C cache-timing-race entry.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit code contract (locked; documented in module docstring)
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_IBKR_CONNECT_FAILED: Final[int] = 4
EXIT_FETCH_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# Defaults (locked clientId conventions per dev-guide §1.5)
# ---------------------------------------------------------------------------

_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

#: Operator-tool clientId for the probe. Must be in 80-99 per dev-guide §1.5
#: LOCKED so it never collides with the order worker (1/2), bar_sync (3), or
#: the recon positions client (4). 88 is the default; override with
#: --client-id if 88 is wedged from a prior probe.
DEFAULT_PROBE_CLIENT_ID: Final[int] = 88

#: ib_gateway host / port — Docker DNS name + paper socat port. Override for
#: live-* (port 4003) per the gnzsnz/ib-gateway-docker convention.
DEFAULT_IBKR_HOST: Final[str] = "ib_gateway"
DEFAULT_IBKR_PORT: Final[int] = 4004

#: Connect handshake budget + per-read-call budget.
CONNECT_TIMEOUT_SECONDS: Final[float] = 30.0
BROKER_CALL_TIMEOUT_SECONDS: Final[float] = 15.0

#: Settle delay between the fresh reqPositions and the re-read of the local
#: cache — long enough to let ib-async fold the snapshot into ib.positions().
DEFAULT_SETTLE_DELAY_SECONDS: Final[float] = 2.0

#: Operator clientId range per dev-guide §1.5 LOCKED.
_OPERATOR_CLIENT_ID_RANGE: Final[range] = range(80, 100)


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests exercise this surface directly so they
    don't have to fight argparse stderr capture."""

    client_id: int
    env: EnvName
    ibkr_host: str
    ibkr_port: int
    account_id: str | None
    settle_delay_seconds: float
    expect_markets: tuple[str, ...]
    allow_non_paper: bool


@dataclass(frozen=True, slots=True)
class ProbePositionRow:
    """One normalized position view row (market + signed quantity + avg cost).

    ``market`` mirrors the adapter's ``_contract_from_ib`` normalization
    (``services/execution/ibkr_adapter.py``): FUT ``secType`` → ``f"/{symbol}"``,
    everything else → ``symbol`` — so the values line up with the backend's
    ``positions_current.market`` convention and ``--expect-markets``.
    """

    market: str
    quantity: Decimal
    avg_cost: Decimal


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.recon_positions_probe",
        description=(
            "Read-only EOD-recon positions diagnostic. Captures the "
            "ib.positions() cache (what the buggy recon reads) vs a fresh "
            "reqPositionsAsync() (what Path B will read) to distinguish a "
            "cache-timing race from phantom positions. Places NO orders."
        ),
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=DEFAULT_PROBE_CLIENT_ID,
        help=(
            f"Probe clientId (default: {DEFAULT_PROBE_CLIENT_ID}). Must be in "
            "80-99 per dev-guide §1.5 LOCKED so it never collides with the "
            "worker (1/2), bar_sync (3), or recon (4)."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Env tag. The probe does no audit/DB writes; this flag mirrors "
            "the other operator-tool CLIs + flips the --allow-non-paper gate "
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
        "--account-id",
        default=None,
        help=(
            "IBKR account number (e.g. U25655583). When omitted the probe "
            "reads the default account on the TWS session — which is what the "
            "recon fetch does when API_IBKR_ACCOUNT_ID is unset."
        ),
    )
    parser.add_argument(
        "--settle-delay-seconds",
        type=float,
        default=DEFAULT_SETTLE_DELAY_SECONDS,
        help=(
            f"Seconds to wait between the fresh reqPositions and the cache "
            f"re-read (default: {DEFAULT_SETTLE_DELAY_SECONDS})."
        ),
    )
    parser.add_argument(
        "--expect-markets",
        default="",
        help=(
            "Optional comma-separated markets the fresh fetch SHOULD list "
            "(e.g. '/M2K,/MES'). When set, the verdict reports which expected "
            "markets were found — a self-contained real-vs-phantom check."
        ),
    )
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        help=(
            "Required for --env in {live-small,live-scale}. The probe is "
            "read-only even on live, but the explicit opt-in keeps the "
            "operator-tool surface consistent."
        ),
    )
    return parser


def _normalize_markets(raw: str) -> tuple[str, ...]:
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def parse_args(argv: list[str]) -> ParsedArgs:
    parsed = _build_parser().parse_args(argv)
    client_id = int(parsed.client_id)
    if client_id not in _OPERATOR_CLIENT_ID_RANGE:
        raise ValueError(
            f"--client-id={client_id} must be in 80-99 per dev-guide §1.5 "
            "(operator-tools range; avoids worker/bar_sync/recon collisions)"
        )
    if parsed.env != "paper" and not parsed.allow_non_paper:
        raise ValueError(
            f"--env={parsed.env} requires --allow-non-paper (read-only, but "
            "explicit opt-in keeps the operator-tool surface consistent)"
        )
    settle = float(parsed.settle_delay_seconds)
    if settle < 0:
        raise ValueError("--settle-delay-seconds must be >= 0")
    return ParsedArgs(
        client_id=client_id,
        env=parsed.env,
        ibkr_host=str(parsed.ibkr_host),
        ibkr_port=int(parsed.ibkr_port),
        account_id=(str(parsed.account_id) if parsed.account_id else None),
        settle_delay_seconds=settle,
        expect_markets=_normalize_markets(str(parsed.expect_markets)),
        allow_non_paper=bool(parsed.allow_non_paper),
    )


def _to_decimal(value: Any) -> Decimal:
    """ib-async reports position / avgCost as float; stringify before Decimal
    so we don't inherit binary-float noise (A05)."""
    return Decimal(str(value))


def _market_from_ib_contract(contract: Any) -> str:
    """Mirror ``IbAsyncIbkrClient._contract_from_ib`` market normalization:
    FUT ``secType`` → ``/{symbol}``; everything else → ``symbol``."""
    symbol = getattr(contract, "symbol", "") or ""
    sec_type = getattr(contract, "secType", "") or ""
    return f"/{symbol}" if sec_type == "FUT" else symbol


def _rows_from_positions(positions: list[Any]) -> tuple[ProbePositionRow, ...]:
    """Normalize ib-async ``Position`` objects → ``ProbePositionRow`` tuple,
    dropping zero-quantity rows (matches the recon planner's view-builders so
    a closed-but-still-listed position doesn't read as held)."""
    rows: list[ProbePositionRow] = []
    for pos in positions:
        qty = _to_decimal(getattr(pos, "position", 0))
        if qty == 0:
            continue
        rows.append(
            ProbePositionRow(
                market=_market_from_ib_contract(getattr(pos, "contract", None)),
                quantity=qty,
                avg_cost=_to_decimal(getattr(pos, "avgCost", 0)),
            )
        )
    return tuple(sorted(rows, key=lambda r: r.market))


def _order_summary(trade: Any) -> dict[str, Any]:
    """Flatten an ib-async ``Trade`` (from reqAllOpenOrders) to a loggable
    dict. Read-only; we never act on these."""
    contract = getattr(trade, "contract", None)
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    return {
        "market": _market_from_ib_contract(contract),
        "action": getattr(order, "action", None),
        "order_type": getattr(order, "orderType", None),
        "total_quantity": str(_to_decimal(getattr(order, "totalQuantity", 0))),
        "aux_price": (
            str(getattr(order, "auxPrice", None))
            if getattr(order, "auxPrice", None) is not None
            else None
        ),
        "lmt_price": (
            str(getattr(order, "lmtPrice", None))
            if getattr(order, "lmtPrice", None) is not None
            else None
        ),
        "status": getattr(status, "status", None),
        "client_id": getattr(order, "clientId", None),
        "order_id": getattr(order, "orderId", None),
        "order_ref": getattr(order, "orderRef", None),
    }


async def _run_probe(args: ParsedArgs) -> int:
    # Lazy import — keeps the unit tests (arg parsing + normalization) free of
    # the real ib_async dependency, same pattern as the adapter.
    from ib_async import IB

    bound = log.bind(
        client_id=args.client_id,
        env=args.env,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port,
        account_id=args.account_id,
        started_at_utc=datetime.now(tz=UTC).isoformat(),
    )
    bound.info("recon_positions_probe_starting")

    ib = IB()
    account = args.account_id or ""
    connected = False
    try:
        try:
            await asyncio.wait_for(
                ib.connectAsync(
                    host=args.ibkr_host,
                    port=args.ibkr_port,
                    clientId=args.client_id,
                    timeout=CONNECT_TIMEOUT_SECONDS,
                ),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            connected = True
        except (Exception, TimeoutError) as exc:
            bound.error("recon_positions_probe_connect_failed", error=repr(exc))
            return EXIT_IBKR_CONNECT_FAILED

        try:
            # View 1 — cache @ connect (what the buggy recon path reads).
            cache_immediate = _rows_from_positions(ib.positions(account=account))
            bound.info(
                "recon_positions_probe_cache_at_connect",
                count=len(cache_immediate),
                positions=[
                    {"market": r.market, "quantity": str(r.quantity)} for r in cache_immediate
                ],
                note=(
                    "ib.positions() read IMMEDIATELY after connect — this is "
                    "exactly what recon's get_positions() reads. A count of 0 "
                    "here with a non-zero fresh count below = the race."
                ),
            )

            # View 2 — fresh reqPositions (awaits positionEnd). Path B's view.
            fresh = _rows_from_positions(
                await asyncio.wait_for(ib.reqPositionsAsync(), timeout=BROKER_CALL_TIMEOUT_SECONDS)
            )
            bound.info(
                "recon_positions_probe_fresh_reqpositions",
                count=len(fresh),
                positions=[
                    {
                        "market": r.market,
                        "quantity": str(r.quantity),
                        "avg_cost": str(r.avg_cost),
                    }
                    for r in fresh
                ],
                note="await reqPositionsAsync() — deterministic; this is what Path B reads.",
            )

            # View 3 — cache @ settle (proves the cache populates given time).
            if args.settle_delay_seconds > 0:
                await asyncio.sleep(args.settle_delay_seconds)
            cache_settled = _rows_from_positions(ib.positions(account=account))
            bound.info(
                "recon_positions_probe_cache_at_settle",
                count=len(cache_settled),
                settle_delay_seconds=args.settle_delay_seconds,
                positions=[
                    {"market": r.market, "quantity": str(r.quantity)} for r in cache_settled
                ],
            )

            # Open orders across ALL clients (protective stops live?).
            open_trades = await asyncio.wait_for(
                ib.reqAllOpenOrdersAsync(), timeout=BROKER_CALL_TIMEOUT_SECONDS
            )
            order_rows = [_order_summary(t) for t in open_trades]
            bound.info(
                "recon_positions_probe_open_orders",
                count=len(order_rows),
                orders=order_rows,
            )
        except (Exception, TimeoutError) as exc:
            bound.error("recon_positions_probe_fetch_failed", error=repr(exc))
            return EXIT_FETCH_FAILED

        # ----- Verdict -----
        fresh_markets = {r.market for r in fresh}
        expected = set(args.expect_markets)
        found_expected = sorted(expected & fresh_markets)
        missing_expected = sorted(expected - fresh_markets)
        race_reproduced = len(cache_immediate) < len(fresh)

        if not fresh:
            verdict = "PHANTOM_OR_FLAT"
            interpretation = (
                "Fresh reqPositions returned ZERO positions. If the backend "
                "still shows open positions_current rows, these are PHANTOM "
                "(backend-vs-broker divergence) — NOT a cache-timing race. Do "
                "NOT apply Path B; escalate to backend-vs-broker cleanup."
            )
        elif expected and missing_expected:
            verdict = "PARTIAL_EXPECTED_MISSING"
            interpretation = (
                f"Fresh reqPositions listed {sorted(fresh_markets)} but "
                f"{missing_expected} were expected and ABSENT. Investigate the "
                "missing markets before applying Path B."
            )
        else:
            verdict = "POSITIONS_REAL_CACHE_RACE"
            interpretation = (
                "Fresh reqPositions lists the open positions; the connect-time "
                "cache was empty/partial. This confirms the cache-timing race "
                "— Path B (await reqPositionsAsync before building the broker "
                "view) is the correct fix."
            )

        bound.info(
            "recon_positions_probe_verdict",
            verdict=verdict,
            cache_at_connect_count=len(cache_immediate),
            fresh_count=len(fresh),
            cache_at_settle_count=len(cache_settled),
            race_reproduced=race_reproduced,
            expected_markets=sorted(expected),
            found_expected=found_expected,
            missing_expected=missing_expected,
            interpretation=interpretation,
        )
        return EXIT_OK
    finally:
        if connected:
            try:
                ib.disconnect()
            except Exception as exc:
                bound.warning("recon_positions_probe_disconnect_failed", error=str(exc))


async def _amain(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            return exc.code if isinstance(exc.code, int) else EXIT_BAD_ARGS
        log.error("recon_positions_probe_bad_args", error=str(exc))
        return EXIT_BAD_ARGS

    try:
        return await _run_probe(args)
    except Exception as exc:  # pragma: no cover - terminal safety net
        log.error(
            "recon_positions_probe_unexpected",
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
    "DEFAULT_PROBE_CLIENT_ID",
    "DEFAULT_SETTLE_DELAY_SECONDS",
    "EXIT_BAD_ARGS",
    "EXIT_FETCH_FAILED",
    "EXIT_IBKR_CONNECT_FAILED",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "ParsedArgs",
    "ProbePositionRow",
    "main",
    "parse_args",
]
