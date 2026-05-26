"""scripts/operator_tools/trigger_v1_cycle.py — on-demand V1 strategy cycle trigger.

Operator-invoked tool that runs the V1 trend-following cycle on demand,
mirroring what ``lean/v1_strategy.py::on_daily_signal_cycle`` does
naturally at 21:30 UTC. Reads bars off the shared ``lean_data`` Docker
volume (no IBKR access), calls
``V1TrendFollowing.generate_signals(...)``, and POSTs each emitted
signal to ``POST /api/internal/lean/signals`` — the same endpoint LEAN
uses. The api owns audit-first ordering + signals-row INSERT per
backend-spec §2.10.1; this tool stays out of the audit-write path.

Motivation
==========

PR #247 (merged 2026-05-26) fixed a Donchian inclusive-window bug that
had suppressed signal firing for 7 days of paper trading. The fix is
deployed, and tomorrow's natural 21:30 UTC LEAN cycle will exercise it.
This tool covers the gap when the operator wants to fire a cycle
outside that scheduled time:

* Test a strategy logic change without waiting overnight.
* Replay a missed cycle (e.g. lean_local was down at 21:30 UTC).
* Manual signal probe for forensic / debugging purposes.
* Diagnostic: "what would the strategy say RIGHT NOW with current data?"

Hard constraints (lifted directly from the operator brief)
==========================================================

* **Risk-state gate.** Aborts unless ``risk_state.state == 'NORMAL'``.
  HALT_NEW + CONVALESCENT both block — this tool is not a backdoor
  around the kill-switch. The operator manually resumes from HALT_NEW
  via the web UI.
* **Dedup against existing signals.** Queries the ``signals`` table for
  ``(account_id, env, market, session_date)`` before POSTing; skips
  markets that already have an emitted row. Prevents double-emission
  when the natural LEAN cycle has partially run today + the operator
  reaches for this tool to fill gaps.
* **V1_SIDELINED_MARKETS honored.** /MCL stays excluded via the
  registry in ``strategies/v1_trend_following/parameters.py`` (set
  difference vs ``V1_CANDIDATE_UNIVERSE``).
* **No IBKR access.** Reads bars from the shared ``lean_data`` Docker
  volume (which ``services/data/bar_sync.py`` writes on its 17:00 ET
  cycle on ``clientId=3``). If a market's bars are stale, the tool
  logs a warning and continues — it does NOT fetch from IBKR. That
  responsibility belongs to ``bar_sync``.
* **--dry-run default = True.** v1 ships with the safety gate ON; the
  operator must pass ``--no-dry-run`` to actually POST. The operator
  brief specifies "dry-run REQUIRED for v1 of the tool" — interpreted
  here as default-on, must-opt-out, so the gate stays visible in every
  invocation.
* **Audit-first ordering is the api's responsibility.** This tool POSTs
  to ``/api/internal/lean/signals``; the endpoint handles audit-first
  ordering per backend-spec §2.10.1 via the canonical
  ``services.qc_adapter.signal_ingestion.ingest_signal_emitted``
  pipeline. The tool itself writes no audit rows.

Bar-loading strategy
====================

**Futures: per-day universe files.** ``services/data/bar_sync.py``
writes one universe file per session_date at ``future/<market_dir>/
universes/<lower>/<YYYYMMDD>.csv``. Each file's single data row
carries the front-month bar for that date (in the format
``<expiry>,<O>,<H>,<L>,<C>,<V>,<OI>``). This is what LEAN's
``DataMappingMode.OPEN_INTEREST`` resolver consumes; we read the same
files directly to construct an equivalent continuous-mapped series.

**Why NOT the zip CSVs:** ``write_futures_bundle`` writes per-expiry
members named ``<lower>_trade_<YYYYMM>.csv`` into the per-ticker zip,
one per contract. The current-front-month member contains
``reqHistoricalDataAsync(ContFuture(...))`` output, but that call only
returns ~175 bars for typical micros (limited by the front-month's
listing-to-now window) — short of the strategy's ``MA_SLOW_DAYS=200``
floor. PR #229's historical-contract backfill ALSO writes per-historical-
contract CSVs to the same zip so LEAN's continuous-contract resolver
can stitch them at roll boundaries. **The v1 of this tool (PR #248)
read only the front-month zip member and hit ``insufficient_bar_history``
for every futures market** — fixed here by switching to universe files
which give 600+ bars going back to bar_sync's earliest cycle. See
``Docs/decisions-log.md`` 2026-05-23 "Historical contract backfill
unblocks MA_SLOW=200" + the same-day's PR #229/#231/#232 trio for the
LEAN-side parity.

**ETFs: single equity-daily CSV (unchanged).** ``write_etf_bundle``
writes a single member named ``<lower>.csv`` with deci-cent integer-
scaled prices (LEAN's equity-daily convention; ``$85.56`` → ``855600``).
This tool decodes the deci-cent scaling back to ``Decimal`` at parse
time. No contract roll → single CSV is sufficient.

Architecture
============

CLI shape::

    python -m scripts.operator_tools.trigger_v1_cycle \\
        --env paper \\
        --session-date 2026-05-26 \\
        [--no-dry-run]

Default session_date is today's ET calendar date.

Forbidden-paths check
=====================

``scripts/operator_tools/**`` is NOT on the dev-guide §11 anti-pattern
[A02] forbidden-modification whitelist; this is pure tooling. The
strategy module (``strategies/v1_trend_following/**``) is called but
not modified. Regular PR review applies; no ``risk-review-approved``
label needed.

A-gates
=======

* **A01 enforced** — every state-mutating write goes through the
  api's ``POST /api/internal/lean/signals`` endpoint, which routes
  through ``ingest_signal_emitted`` → ``append_audit_event``. This
  tool itself writes nothing to the DB and emits no audit events.
* **A05 enforced** — Decimal at every price / equity boundary;
  serialized as strings in the HTTP payload per dev-guide §3.8.
* **A06 enforced** — tz-aware UTC at every datetime boundary; the
  api's Pydantic schema re-validates.
* **A22 N/A** — unit tests stub the DB session factory + the bar-zip
  files via fixtures; no live api / Postgres / IBKR.
* **A27 satisfied** — operator runbook at
  ``scripts/operator_tools/README.md``.

Exit codes
==========

* ``0`` — success: cycle ran cleanly; all eligible signals POSTed
  (or marked would-POST in --dry-run); summary logged.
* ``1`` — risk_state blocked dispatch (not NORMAL). Operator's
  recourse is to investigate why the state machine is in HALT_NEW /
  CONVALESCENT and (if appropriate) manually resume via /system.
* ``3`` — at least one signal HTTP POST returned a non-2xx status
  AND we were not in --dry-run. Other signals may have landed
  cleanly; check the per-signal log lines + the audit_log for the
  ones that did succeed.
* ``5`` — DB init failure (DATABASE_URL missing / wrong / Postgres
  unreachable).
* ``6`` — invalid CLI args.
* ``7`` — required env var missing (LEAN_LOCAL_BEARER_TOKEN unset
  AND not --dry-run).
* ``99`` — unexpected exception (logged with traceback).

Usage
=====

CLI invocation (typically via the operator runbook in
``scripts/operator_tools/README.md``)::

    python -m scripts.operator_tools.trigger_v1_cycle --env paper

Required env vars (when not in --dry-run; staged by the operator
runbook before invocation):

* ``DATABASE_URL`` — ``postgresql+asyncpg://app_service:...@postgres:5432/trading``
* ``LEAN_LOCAL_BEARER_TOKEN`` — same bearer the lean_local container
  uses; sops-encrypted in ``secrets/<env>.enc.yaml::lean.api_bearer_token``.

Optional env vars:

* ``TRIGGER_V1_CYCLE_API_URL`` — override the api URL (default:
  ``http://api:8000``).
* ``TRIGGER_V1_CYCLE_LEAN_DATA_ROOT`` — override the LEAN data root
  (default: ``/Lean/Data``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import structlog

from strategies.v1_trend_following.parameters import (
    V1_CANDIDATE_UNIVERSE,
    V1_SIDELINED_MARKETS,
    V1Parameters,
    default_v1_parameters,
)
from strategies.v1_trend_following.signals import (
    Bar,
    BarSeries,
    CandidateSignal,
    Direction,
    Position,
)
from strategies.v1_trend_following.strategy import STRATEGY_NAME, V1TrendFollowing

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit code contract (locked; documented in module docstring + README)
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_RISK_STATE_BLOCKED: Final[int] = 1
EXIT_POST_FAILED: Final[int] = 3
EXIT_DB_INIT_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_BEARER_MISSING: Final[int] = 7
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

#: Env values accepted on the CLI. Mirror of ``audit_log.env`` CHECK
#: constraint (``alembic/versions/0001_audit_log.py``). "dev" is NOT
#: valid — the api endpoint uses the canonical ``Environment`` Literal
#: from ``services.audit.writer`` which only permits paper / live-small
#: / live-scale.
_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

DATABASE_URL_ENV: Final[str] = "DATABASE_URL"
LEAN_BEARER_ENV: Final[str] = "LEAN_LOCAL_BEARER_TOKEN"
API_URL_ENV: Final[str] = "TRIGGER_V1_CYCLE_API_URL"
DATA_ROOT_ENV: Final[str] = "TRIGGER_V1_CYCLE_LEAN_DATA_ROOT"

DEFAULT_API_BASE_URL: Final[str] = "http://api:8000"
DEFAULT_DATA_ROOT: Final[Path] = Path("/Lean/Data")
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 15.0

#: Number of trailing daily bars considered "fresh enough" for the cycle.
#: Bar_sync's freshness threshold for warning is also 5 days
#: (``DEFAULT_STALENESS_THRESHOLD_DAYS`` in
#: ``strategies/v1_trend_following/universe_freshness.py``). We warn at
#: the same threshold here but DO NOT abort — the operator may
#: deliberately want to trigger a cycle against last-known data while
#: bar_sync is still recovering.
BAR_STALENESS_WARN_DAYS: Final[int] = 5

#: Locked strategy version identifier for tool-triggered signals. Visually
#: distinct from LEAN's ``v1_trend_following@phase1-pivot-d`` so the
#: operator can filter audit_log + the /signals page to see which were
#: tool-triggered vs naturally-scheduled. The api's
#: ``_derive_strategy_hash`` in ``services/qc_adapter/signal_ingestion.py``
#: sha1's any non-40-hex suffix into a deterministic 40-char hash, so
#: audit-chain integrity is preserved.
STRATEGY_VERSION_MARKER: Final[str] = f"{STRATEGY_NAME}@operator-trigger"

_ET: Final[ZoneInfo] = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests use this surface directly so they
    don't fight stderr capture on argparse SystemExit paths.
    """

    env: EnvName
    session_date: date
    dry_run: bool
    data_root: Path
    api_base_url: str
    http_timeout_seconds: float
    allow_non_paper: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.trigger_v1_cycle",
        description=(
            "Run the V1 trend-following strategy cycle on demand. Reads "
            "bars from the shared lean_data volume, calls the strategy, "
            "and POSTs each emitted signal to the api's "
            "/api/internal/lean/signals endpoint (same endpoint LEAN "
            "uses at 21:30 UTC). Safety: risk-state gate, dedup against "
            "already-emitted signals, --dry-run default ON, "
            "V1_SIDELINED_MARKETS honored, no IBKR access. See "
            "scripts/operator_tools/README.md for the runbook."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Env tag for the signal POST payloads — mirrors audit_log.env "
            "CHECK. Live-* envs require the --allow-non-paper safety "
            "gate (fail-closed default for tooling that touches "
            "production signal flow)."
        ),
    )
    parser.add_argument(
        "--session-date",
        default=None,
        help=(
            "CME session date (YYYY-MM-DD, ET calendar). Defaults to "
            "today's ET date if omitted. Drives the strategy's "
            "as_of_session_date + the dedup check against the signals "
            "table. Pass a past date to replay a missed cycle."
        ),
    )
    # --dry-run / --no-dry-run pair so the safety default is visible at
    # every invocation. argparse's BooleanOptionalAction does this for us
    # in Python 3.9+; the operator must pass --no-dry-run to actually POST.
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "If set (default), log what WOULD be POSTed without actually "
            "POSTing. The operator must pass --no-dry-run to commit. "
            "Risk-state gate, parameter load, position snapshot, "
            "strategy invocation, and dedup check all still run."
        ),
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help=(
            "LEAN on-disk data root. Default reads from "
            f"{DATA_ROOT_ENV} env var or {DEFAULT_DATA_ROOT!s}. Tests "
            "override to a tmp_path fixture."
        ),
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help=(
            "Base URL of the api service. Default reads from "
            f"{API_URL_ENV} env var or {DEFAULT_API_BASE_URL!r} "
            "(Docker DNS — same target lean_local uses). Tests "
            "override to a stub server."
        ),
    )
    parser.add_argument(
        "--http-timeout-seconds",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        help=(
            f"Per-POST HTTP timeout. Default: "
            f"{DEFAULT_HTTP_TIMEOUT_SECONDS}s (matches LEAN's _post_event "
            "timeout)."
        ),
    )
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        default=False,
        help=(
            "Required when --env is live-small or live-scale. The tool "
            "fails closed without this flag for any non-paper env "
            "(tool drives the same audit-write path the natural cycle "
            "does)."
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
            "for tooling that drives production signal flow)"
        )

    if args.session_date is None:
        session_date = datetime.now(tz=UTC).astimezone(_ET).date()
    else:
        try:
            session_date = datetime.strptime(args.session_date, "%Y-%m-%d").date()
        except ValueError:
            parser.error(f"--session-date={args.session_date!r} is not a valid YYYY-MM-DD date")

    data_root_raw = args.data_root or os.environ.get(DATA_ROOT_ENV) or str(DEFAULT_DATA_ROOT)
    data_root = Path(data_root_raw)

    api_base_url = args.api_url or os.environ.get(API_URL_ENV) or DEFAULT_API_BASE_URL

    if args.http_timeout_seconds <= 0:
        parser.error("--http-timeout-seconds must be > 0")

    return ParsedArgs(
        env=env,
        session_date=session_date,
        dry_run=bool(args.dry_run),
        data_root=data_root,
        api_base_url=api_base_url,
        http_timeout_seconds=float(args.http_timeout_seconds),
        allow_non_paper=bool(args.allow_non_paper),
    )


# ---------------------------------------------------------------------------
# Active universe (pure)
# ---------------------------------------------------------------------------


def compute_active_universe() -> tuple[str, ...]:
    """V1_CANDIDATE_UNIVERSE minus V1_SIDELINED_MARKETS.

    The set difference is the active set this tool iterates over.
    V1_CANDIDATE_UNIVERSE already excludes /MCL by construction
    (see ``strategies/v1_trend_following/parameters.py``), so this set
    operation is defense-in-depth — if a future PR re-adds /MCL to
    V1_CANDIDATE_UNIVERSE without also clearing it from
    V1_SIDELINED_MARKETS, this tool stays correct.

    Returns markets in V1_CANDIDATE_UNIVERSE order (insertion order
    preserved per Python 3.7+ tuple semantics).
    """
    return tuple(m for m in V1_CANDIDATE_UNIVERSE if m not in V1_SIDELINED_MARKETS)


# ---------------------------------------------------------------------------
# Bar reading (pure; tested via test fixtures)
# ---------------------------------------------------------------------------


def _parse_yyyymmdd_to_date(token: str) -> date:
    """Parse a ``YYYYMMDD`` token to a ``date`` instance."""
    return datetime.strptime(token, "%Y%m%d").date()


def _parse_universe_file_bar(file_bytes: bytes, session_date: date) -> Bar | None:
    """Parse one bar_sync universe file → ``Bar`` (raw front-month prices).

    Format per ``services/data/bar_sync.py::build_futures_universe_csv``:

        #expiry,open,high,low,close,volume,open_interest
        <expiry>,<O>,<H>,<L>,<C>,<V>,<OI>

    The data row's bar IS the front-month bar for ``session_date`` (the
    expiry tag tells you WHICH contract was active that day; for
    strategy purposes we only need the OHLCV). ``session_date`` comes
    from the FILENAME — the data row doesn't carry a timestamp.

    Returns None on any parse failure (empty file, header-only, malformed
    cell, ``close <= 0`` sentinel). Defensive — bar_sync's writer
    produces well-formed files, but this function runs against on-disk
    data which could be from a prior buggy bar_sync revision OR a
    half-written file mid-cycle.
    """
    data_row: str | None = None
    for line in file_bytes.decode("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        data_row = stripped
        break
    if data_row is None:
        return None
    parts = data_row.split(",")
    if len(parts) < 6:
        return None
    try:
        # parts[0] = expiry (informational; tells you which contract; we
        # don't need it for strategy evaluation since the prices are
        # absolute per-contract under RAW mode). decimal.InvalidOperation
        # is NOT a ValueError subclass, so we catch it explicitly for
        # malformed numeric cells.
        open_v = Decimal(parts[1].strip())
        high_v = Decimal(parts[2].strip())
        low_v = Decimal(parts[3].strip())
        close_v = Decimal(parts[4].strip())
        volume = int(parts[5].strip())
    except (ValueError, IndexError, InvalidOperation):
        return None
    if close_v <= 0:
        return None
    if volume < 0:
        volume = 0
    return Bar(
        session_date=session_date,
        open=open_v,
        high=high_v,
        low=low_v,
        close=close_v,
        volume=volume,
    )


def _parse_etf_csv_row(row: str) -> Bar | None:
    """One row of ``YYYYMMDD HH:MM,O*10000,H*10000,L*10000,C*10000,V`` → ``Bar``.

    Inverse of ``bar_sync._equity_daily_csv_line`` — divides each price
    cell by 10000 to recover the original Decimal. Returns None on any
    parse failure (same shape as ``_parse_universe_file_bar``).
    """
    parts = row.strip().split(",")
    if len(parts) < 6:
        return None
    timestamp_token = parts[0].strip().split()[0] if parts[0].strip() else ""
    if not timestamp_token:
        return None
    try:
        session_date = _parse_yyyymmdd_to_date(timestamp_token)
        # Deci-cent scaling: ``$85.5600`` → ``855600``. Divide by 10000
        # exactly via Decimal to preserve precision (float division would
        # introduce binary-float drift on certain prices).
        scale = Decimal("10000")
        open_v = Decimal(parts[1].strip()) / scale
        high_v = Decimal(parts[2].strip()) / scale
        low_v = Decimal(parts[3].strip()) / scale
        close_v = Decimal(parts[4].strip()) / scale
        volume = int(parts[5].strip())
    except (ValueError, IndexError):
        return None
    if close_v <= 0:
        return None
    if volume < 0:
        volume = 0
    return Bar(
        session_date=session_date,
        open=open_v,
        high=high_v,
        low=low_v,
        close=close_v,
        volume=volume,
    )


def read_etf_bars_from_zip(zip_path: Path, ticker: str) -> list[Bar]:
    """Read deci-cent-scaled equity-daily bars from a bar_sync ETF zip.

    Returns a chronologically-sorted, deduped list (last writer wins
    on duplicate session_date, mirroring
    ``bar_sync.parse_ibkr_bars``). Returns empty list if the zip is
    missing, unreadable, or the expected member isn't present.
    """
    if not zip_path.is_file():
        log.warning(
            "trigger_v1_cycle_etf_zip_missing",
            ticker=ticker,
            zip_path=str(zip_path),
        )
        return []
    member_name = f"{ticker.lower()}.csv"
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if member_name not in zf.namelist():
                log.warning(
                    "trigger_v1_cycle_etf_member_missing",
                    ticker=ticker,
                    zip_path=str(zip_path),
                    expected_member=member_name,
                    namelist=zf.namelist(),
                )
                return []
            raw_bytes = zf.read(member_name)
    except (zipfile.BadZipFile, OSError) as exc:
        log.error(
            "trigger_v1_cycle_etf_zip_read_failed",
            ticker=ticker,
            zip_path=str(zip_path),
            error=str(exc),
            error_class=type(exc).__name__,
        )
        return []
    seen: dict[date, Bar] = {}
    for row in raw_bytes.decode("utf-8").splitlines():
        if not row.strip():
            continue
        bar = _parse_etf_csv_row(row)
        if bar is None:
            continue
        seen[bar.session_date] = bar
    return sorted(seen.values(), key=lambda b: b.session_date)


#: Default number of trailing calendar days of universe files to read
#: per futures market. The V1 strategy's MA_SLOW_DAYS=200 (trading days)
#: → ~280 calendar days (200 / 0.71 weekday ratio). 365 gives comfortable
#: headroom against bar_sync gaps (holiday clusters, prior cycle misses).
#: Each universe file is a single bar, so the cost is ~365 small-file
#: reads per futures market per cycle — bounded + fast.
DEFAULT_FUTURES_UNIVERSE_LOOKBACK_DAYS: Final[int] = 365


def read_futures_bars_from_universe_files(
    universes_dir: Path,
    ticker: str,
    *,
    as_of: date,
    lookback_days: int = DEFAULT_FUTURES_UNIVERSE_LOOKBACK_DAYS,
) -> list[Bar]:
    """Read continuous-mapped futures bars by walking per-day universe files.

    ``services/data/bar_sync.py::write_futures_bundle`` writes one
    universe file per session_date at ``future/<market_dir>/universes/
    <lower>/<YYYYMMDD>.csv``. Each file's single data row is the
    front-month bar for that date (the contract picked by LEAN's
    ``DataMappingMode.OPEN_INTEREST`` resolver). Reading these directly
    replicates LEAN's continuous-mapped series ⇒ ≥200 bars available
    across ~365 calendar days for the V1 ``MA_SLOW_DAYS=200`` floor.

    Why not the zip CSVs (the v1 of this tool, PR #248): the zip's
    front-month member (``<lower>_trade_<current_yyyymm>.csv``) only
    contains the current contract's lifetime-to-date (~175 bars for
    typical micros) → ``insufficient_bar_history`` for every futures
    market. PR #229's historical-contract backfill writes per-contract
    CSVs that LEAN stitches, but stitching by hand from those CSVs is
    error-prone (per-contract date ranges overlap; the "active" contract
    per date depends on OI rather than calendar). The universe files
    are LEAN's canonical answer to "which contract was active on this
    date + what was its OHLCV?" — we just consume the same artifact.

    Walks ``universes_dir``, filters to files in ``(as_of -
    lookback_days, as_of]``, parses each into a Bar, returns
    chronologically-sorted list. Returns empty list if the directory is
    missing, unreadable, or contains no parseable files in the window.
    """
    if not universes_dir.is_dir():
        log.warning(
            "trigger_v1_cycle_futures_universes_dir_missing",
            ticker=ticker,
            universes_dir=str(universes_dir),
        )
        return []
    earliest = as_of - timedelta(days=lookback_days)
    bars: list[Bar] = []
    file_count = 0
    skipped_unparseable = 0
    try:
        entries = list(universes_dir.iterdir())
    except OSError as exc:
        log.error(
            "trigger_v1_cycle_futures_universes_dir_read_failed",
            ticker=ticker,
            universes_dir=str(universes_dir),
            error=str(exc),
            error_class=type(exc).__name__,
        )
        return []
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".csv":
            continue
        try:
            session_date = _parse_yyyymmdd_to_date(entry.stem)
        except ValueError:
            continue
        if session_date < earliest or session_date > as_of:
            continue
        try:
            file_bytes = entry.read_bytes()
        except OSError as exc:
            log.warning(
                "trigger_v1_cycle_universe_file_read_failed",
                ticker=ticker,
                file=str(entry),
                error=str(exc),
            )
            continue
        bar = _parse_universe_file_bar(file_bytes, session_date)
        if bar is None:
            skipped_unparseable += 1
            continue
        bars.append(bar)
        file_count += 1
    bars.sort(key=lambda b: b.session_date)
    log.info(
        "trigger_v1_cycle_futures_universe_files_loaded",
        ticker=ticker,
        bars_loaded=file_count,
        skipped_unparseable=skipped_unparseable,
        oldest_session_date=(bars[0].session_date.isoformat() if bars else None),
        newest_session_date=(bars[-1].session_date.isoformat() if bars else None),
        lookback_days=lookback_days,
        as_of_session_date=as_of.isoformat(),
    )
    return bars


def load_bars_for_market(market: str, data_root: Path, *, as_of: date) -> list[Bar]:
    """Dispatch to the ETF or futures bar reader for a single market.

    Keys mirror ``V1_CANDIDATE_UNIVERSE``: ``/MES``-style for futures,
    bare ticker (``TLT``) for ETFs. Path computation mirrors
    ``services/data/bar_sync.py``'s ``equity_daily_zip_path`` so on-disk
    layout stays consistent between writer + reader. Futures use the
    per-day universe files at ``future/<market_dir>/universes/<lower>/``
    rather than the per-expiry zip members so the read replicates
    LEAN's continuous-mapped series (see
    :func:`read_futures_bars_from_universe_files` for the rationale).

    ``as_of`` is the cycle's ``session_date``; futures reads anchor on
    this to bound the lookback window. ETFs ignore it (single CSV
    contains the full history).

    For markets not in the locked PHASE1_UNIVERSE_METADATA, this
    function returns an empty list with a warning (silently dropping
    would mask a typo in V1_CANDIDATE_UNIVERSE).
    """
    # Lazy import to avoid coupling this script to bar_sync's import
    # graph at module load (bar_sync pulls in zoneinfo + sqlalchemy
    # transitively, both of which are fine in production but make the
    # test surface heavier than it needs to be).
    from services.data.bar_sync import (
        PHASE1_UNIVERSE_METADATA,
        equity_daily_zip_path,
    )

    meta = PHASE1_UNIVERSE_METADATA.get(market)
    if meta is None:
        log.warning(
            "trigger_v1_cycle_unknown_market_in_metadata",
            market=market,
            available_markets=sorted(PHASE1_UNIVERSE_METADATA.keys()),
        )
        return []
    if meta.kind == "etf":
        return read_etf_bars_from_zip(
            equity_daily_zip_path(data_root, meta.ibkr_symbol),
            ticker=meta.ibkr_symbol,
        )
    # Futures — meta.kind == "futures". Universe files mirror what
    # bar_sync writes (per-day, front-month bar tagged with active
    # contract expiry). Path: future/<market_dir>/universes/<lower>/.
    universes_dir = data_root / "future" / meta.market_dir / "universes" / meta.ibkr_symbol.lower()
    return read_futures_bars_from_universe_files(
        universes_dir,
        ticker=meta.ibkr_symbol,
        as_of=as_of,
    )


# ---------------------------------------------------------------------------
# Bar staleness check (pure)
# ---------------------------------------------------------------------------


def is_bar_series_stale(
    *,
    bars: Sequence[Bar],
    as_of: date,
    threshold_days: int = BAR_STALENESS_WARN_DAYS,
) -> bool:
    """Return True if the newest bar is more than ``threshold_days`` old.

    Bar_sync runs daily at 17:00 ET; a newest-bar-date older than
    today minus ``threshold_days`` calendar days indicates bar_sync
    has been missing cycles. This is a warning condition only — the
    operator may deliberately want to trigger against last-known
    data — but we surface the warning so it's not silent.
    """
    if not bars:
        return True
    newest = bars[-1].session_date
    return (as_of - newest).days > threshold_days


# ---------------------------------------------------------------------------
# V1Parameters from JSONB (pure)
# ---------------------------------------------------------------------------


def build_v1_parameters_from_dict(raw: dict[str, object]) -> V1Parameters:
    """Construct ``V1Parameters`` from the ``parameter_sets.parameters`` JSONB.

    Keys are the canonical SCREAMING_SNAKE_CASE the V1 strategy persists
    (see ``V1_DEFAULTS`` in ``strategies/v1_trend_following/parameters.py``).
    Decimal-as-string per dev-guide §3.8 / A05 — every numeric field
    coerces through ``str()`` to preserve precision through the JSONB
    boundary.

    Falls back to ``default_v1_parameters()`` if the dict is empty
    (zero-row parameter_sets state — bootstrap migration hasn't run yet).
    Raises ``KeyError`` if the dict is non-empty but missing a required
    key (programming error — surfaces a parameter-set schema drift).
    """
    if not raw:
        return default_v1_parameters()
    # Coerce via str() — JSONB values land here as ``object`` (could be
    # int / str depending on driver), but the canonical V1_DEFAULTS keys
    # are all int-or-Decimal-string-able. ``int(str(...))`` round-trips
    # both shapes cleanly while keeping mypy --strict happy.
    return V1Parameters(
        lookback_days_donchian=int(str(raw["LOOKBACK_DAYS_DONCHIAN"])),
        ma_fast_days=int(str(raw["MA_FAST_DAYS"])),
        ma_slow_days=int(str(raw["MA_SLOW_DAYS"])),
        hurst_threshold=Decimal(str(raw["HURST_THRESHOLD"])),
        stop_distance_atr_mult=Decimal(str(raw["STOP_DISTANCE_ATR_MULT"])),
        atr_lookback_days=int(str(raw["ATR_LOOKBACK_DAYS"])),
        min_holding_days=int(str(raw["MIN_HOLDING_DAYS"])),
        vol_target_pct_annual=Decimal(str(raw["VOL_TARGET_PCT_ANNUAL"])),
        instrument_vol_lookback_days=int(str(raw["INSTRUMENT_VOL_LOOKBACK_DAYS"])),
        roll_days_before_expiry=int(str(raw["ROLL_DAYS_BEFORE_EXPIRY"])),
    )


# ---------------------------------------------------------------------------
# Position construction from DB row (pure)
# ---------------------------------------------------------------------------


def build_position_from_row(
    *,
    market: str,
    quantity: int,
    avg_cost: Decimal,
    opened_at_utc: datetime | None,
) -> Position:
    """Translate a ``positions_current`` row + optional trade open date → Position.

    Quantity sign drives direction (positive = LONG, negative = SHORT,
    zero = FLAT). ``opened_at_utc`` is sourced from the open ``trades``
    row when one exists; None when no open trade is recorded (the
    strategy's MIN_HOLDING_DAYS check is then conservatively skipped
    per ``strategies/v1_trend_following/strategy.py::_evaluate_market``).
    """
    if quantity > 0:
        direction = Direction.LONG
    elif quantity < 0:
        direction = Direction.SHORT
    else:
        direction = Direction.FLAT

    opened_at_session_date: date | None = None
    if direction is not Direction.FLAT and opened_at_utc is not None:
        opened_at_session_date = opened_at_utc.astimezone(_ET).date()

    return Position(
        market=market,
        direction=direction,
        quantity=quantity,
        avg_cost=avg_cost,
        opened_at_session_date=opened_at_session_date,
    )


# ---------------------------------------------------------------------------
# Minimal Stage 0 sizing trace (mirrors lean/v1_strategy.py)
# ---------------------------------------------------------------------------


def build_minimal_sizing_trace(
    *,
    signal: CandidateSignal,
    target_contracts: int,
    equity: Decimal,
) -> dict[str, Any]:
    """Construct a Stage 0-shaped sizing_trace mirroring LEAN's payload.

    Matches the structure produced by
    ``lean/v1_strategy.py::_build_minimal_sizing_trace`` so the
    backend's ``ingest_signal_emitted`` pipeline accepts identical
    payloads from either source. Stages 1-5 are intentionally absent
    (the server-side risk engine populates them at dispatch time when
    the operator approves the signal).
    """
    snapshot = signal.indicators_snapshot
    strategy_inputs = {
        signal.market: {
            "donchian_high": str(snapshot["donchian_high"]),
            "donchian_low": str(snapshot["donchian_low"]),
            "ma_fast": str(snapshot["ma_fast"]),
            "ma_slow": str(snapshot["ma_slow"]),
            "hurst": str(snapshot["hurst"]),
            "atr": str(snapshot["atr"]),
            "stop_price": str(signal.stop_price),
            "lookback_days_donchian": int(snapshot["lookback_days_donchian"]),
        }
    }
    return {
        "schema_version": 1,
        "stage_0_universe": {
            "active_markets": [signal.market],
            "excluded": [],
            "strategy_inputs": strategy_inputs,
        },
        "lean_naive_sizing": {
            "target_contracts": target_contracts,
            "equity_usd": str(equity),
            "rationale": (
                "Operator-trigger conservative single-lot allocation pending "
                "server-side Stage 0-5 sizing (Worker-PR-1)."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Signal payload assembly (pure)
# ---------------------------------------------------------------------------


def build_signal_post_payload(
    *,
    signal: CandidateSignal,
    session_date: date,
    equity: Decimal,
    target_contracts: int,
    strategy_version: str,
    emitted_at_utc: datetime,
) -> dict[str, Any]:
    """Compose the ``LeanEventRequest``-shaped POST body for one signal.

    Field shape MUST match ``services/api/schemas/lean.py::LeanEventRequest``
    + the route-side required-field gate in
    ``services/api/routes/internal/lean.py::post_lean_signal``. The api
    rejects unknown fields (Pydantic ``extra='forbid'``); fields must
    be exactly the LEAN-emit set.

    decision_price is serialized as a STRING per A05 — the api's
    Pydantic schema accepts ``Decimal | None`` so str is the canonical
    on-wire form (Decimal's JSON serialization rounds; str preserves
    full precision).
    """
    sizing_trace = build_minimal_sizing_trace(
        signal=signal,
        target_contracts=target_contracts,
        equity=equity,
    )
    return {
        "event_type": "signal_emitted",
        "ts_utc": emitted_at_utc.isoformat(),
        "algorithm_id": STRATEGY_NAME,
        "session_date_et": session_date.isoformat(),
        "equity_usd": str(equity),
        "live_mode": True,
        "market": signal.market,
        "direction": signal.direction.value,
        "target_contracts": target_contracts,
        "decision_price": str(signal.decision_price),
        "sizing_trace": sizing_trace,
        "strategy_version": strategy_version,
    }


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CycleContext:
    """Snapshot of DB state needed to run one cycle.

    Loaded once per invocation by ``load_cycle_context``; passed by
    value into ``run_cycle`` so the orchestration is testable without
    a live DB.
    """

    account_id: UUID
    risk_state: str
    parameters: V1Parameters
    current_positions: dict[str, Position]
    already_emitted_markets: frozenset[str]
    latest_balance_nav: Decimal


async def fetch_account_id(session: Any) -> UUID | None:
    """SELECT id FROM accounts WHERE active_to IS NULL ORDER BY active_from ASC LIMIT 1.

    Mirrors ``PostgresPhase1QueryRepo.fetch_active_account_id``; we
    don't reuse the repo class because the script runs as a one-shot
    with its own session_factory + we want to keep the import graph
    minimal.
    """
    from sqlalchemy import text

    row = (
        await session.execute(
            text("SELECT id FROM accounts WHERE active_to IS NULL ORDER BY active_from ASC LIMIT 1")
        )
    ).fetchone()
    return UUID(str(row.id)) if row else None


async def fetch_risk_state_for_account(session: Any, account_id: UUID) -> str | None:
    """SELECT state FROM risk_state WHERE account_id=:acct AND is_current=TRUE."""
    from sqlalchemy import text

    row = (
        await session.execute(
            text("SELECT state FROM risk_state WHERE account_id = :acct AND is_current = TRUE"),
            {"acct": account_id},
        )
    ).fetchone()
    return str(row.state) if row else None


async def fetch_active_parameters_dict(session: Any) -> dict[str, object]:
    """SELECT parameters FROM parameter_sets WHERE last_active_at IS NULL ...

    Returns the head pointer's JSONB ``parameters`` column as a dict.
    Empty dict if no parameter_set row exists (fresh deploy) —
    ``build_v1_parameters_from_dict`` falls back to V1_DEFAULTS in
    that case.
    """
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT parameters FROM parameter_sets "
                "WHERE last_active_at IS NULL "
                "ORDER BY first_active_at DESC LIMIT 1"
            )
        )
    ).fetchone()
    if row is None:
        return {}
    return dict(row.parameters)


async def fetch_positions_for_v1_markets(
    session: Any,
    *,
    account_id: UUID,
    markets: tuple[str, ...],
) -> dict[str, Position]:
    """SELECT positions_current LEFT JOIN open trades for the V1 active universe.

    The join lets us populate ``opened_at_session_date`` from the
    matching open ``trades`` row when one exists; if no open trade is
    recorded, the position's open date stays None and the strategy's
    MIN_HOLDING_DAYS check is conservatively skipped (per the strategy's
    own ``_evaluate_market``).

    Markets with quantity=0 are dropped — the strategy treats absence
    as FLAT (``current_positions.get(market)`` returns None).
    """
    if not markets:
        return {}
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT p.market, p.quantity, p.avg_cost, "
                "       (SELECT MIN(t.opened_at_utc) FROM trades t "
                "          WHERE t.account_id = p.account_id "
                "            AND t.market = p.market "
                "            AND t.state = 'open_position') AS opened_at_utc "
                "FROM positions_current p "
                "WHERE p.account_id = :acct "
                "  AND p.market = ANY(:markets) "
                "  AND p.quantity <> 0"
            ),
            {"acct": account_id, "markets": list(markets)},
        )
    ).fetchall()
    positions: dict[str, Position] = {}
    for row in rows:
        positions[row.market] = build_position_from_row(
            market=row.market,
            quantity=int(row.quantity),
            avg_cost=Decimal(str(row.avg_cost)),
            opened_at_utc=row.opened_at_utc,
        )
    return positions


async def fetch_already_emitted_markets(
    session: Any,
    *,
    account_id: UUID,
    env: EnvName,
    session_date: date,
) -> frozenset[str]:
    """SELECT signals.market for the (account, env, session_date) tuple.

    The dedup key. Any market that already has a signal row for this
    session_date is skipped by the orchestrator — typically because
    today's natural 21:30 UTC LEAN cycle has already emitted it.

    Returns a frozenset so callers can use ``in`` cheaply.
    """
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT market FROM signals "
                "WHERE account_id = :acct "
                "  AND env = :env "
                "  AND session_date = :sd"
            ),
            {"acct": account_id, "env": env, "sd": session_date},
        )
    ).fetchall()
    return frozenset(row.market for row in rows)


async def fetch_latest_balance_nav(session: Any, account_id: UUID) -> Decimal | None:
    """SELECT net_liquidation FROM balances WHERE account_id=:acct ORDER BY snapshot_ts DESC LIMIT 1.

    Mirrors ``PostgresPhase1QueryRepo.fetch_latest_balance_nav``. Used
    as the ``equity`` field in the signal payload; the LEAN cycle
    sources this from ``self.portfolio.total_portfolio_value`` which
    is the same value the EOD reconciliation derives net_liquidation
    from.
    """
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT net_liquidation FROM balances "
                "WHERE account_id = :acct "
                "ORDER BY snapshot_ts DESC LIMIT 1"
            ),
            {"acct": account_id},
        )
    ).fetchone()
    return Decimal(str(row.net_liquidation)) if row else None


async def load_cycle_context(
    session_factory: Any,
    *,
    env: EnvName,
    session_date: date,
    active_universe: tuple[str, ...],
) -> CycleContext | None:
    """One-shot DB load for everything the orchestrator needs.

    Returns None if no active account exists (fresh-deploy state; the
    bootstrap migration hasn't seeded accounts yet). Raises if any
    other query errors.
    """
    async with session_factory() as session:
        account_id = await fetch_account_id(session)
        if account_id is None:
            return None
        risk_state = await fetch_risk_state_for_account(session, account_id)
        params_dict = await fetch_active_parameters_dict(session)
        positions = await fetch_positions_for_v1_markets(
            session,
            account_id=account_id,
            markets=active_universe,
        )
        already_emitted = await fetch_already_emitted_markets(
            session,
            account_id=account_id,
            env=env,
            session_date=session_date,
        )
        balance_nav = await fetch_latest_balance_nav(session, account_id)
    return CycleContext(
        account_id=account_id,
        risk_state=risk_state or "<missing>",
        parameters=build_v1_parameters_from_dict(params_dict),
        current_positions=positions,
        already_emitted_markets=already_emitted,
        latest_balance_nav=balance_nav if balance_nav is not None else Decimal("0"),
    )


# ---------------------------------------------------------------------------
# HTTP POST
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalPostOutcome:
    """Per-signal POST result."""

    market: str
    status: Literal["posted", "dedup_skipped", "dry_run_skipped", "post_failed"]
    http_status_code: int | None = None
    signal_id: str | None = None
    audit_event_uuid: str | None = None
    error: str | None = None


async def post_signal(
    *,
    http_client: httpx.AsyncClient,
    api_base_url: str,
    bearer_token: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """POST one signal payload; return (status_code, response_json).

    Raises ``httpx.HTTPError`` on network / timeout failure; the caller
    wraps + classifies. Non-2xx responses come back with the parsed
    JSON body (or an empty dict if the body wasn't JSON) so the caller
    can log the api's error envelope.
    """
    url = f"{api_base_url.rstrip('/')}/api/internal/lean/signals"
    response = await http_client.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "User-Agent": "trigger_v1_cycle/0.1",
        },
    )
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = {}
    return response.status_code, body


# ---------------------------------------------------------------------------
# Per-market orchestration (pure structure; HTTP injectable for tests)
# ---------------------------------------------------------------------------


async def emit_signal_with_dedup(
    *,
    signal: CandidateSignal,
    session_date: date,
    equity: Decimal,
    already_emitted: frozenset[str],
    strategy_version: str,
    emitted_at_utc: datetime,
    dry_run: bool,
    http_client: httpx.AsyncClient | None,
    api_base_url: str,
    bearer_token: str | None,
) -> SignalPostOutcome:
    """End-to-end per-signal POST flow with dedup + dry-run gates.

    Order:
      1. Dedup check (skip if market already has a signal row for
         this session_date)
      2. Build payload
      3. Dry-run? log + skip
      4. POST + classify response

    target_contracts is hard-coded to 1 (conservative single-lot
    allocation; matches LEAN's
    ``lean/v1_strategy.py::_naive_target_contracts``). The full
    Stage 0-5 sizing pipeline runs server-side when the operator
    approves the signal.
    """
    if signal.market in already_emitted:
        log.info(
            "trigger_v1_cycle_dedup_skip",
            market=signal.market,
            session_date=session_date.isoformat(),
            reason="market already has signal row for this session_date",
        )
        return SignalPostOutcome(market=signal.market, status="dedup_skipped")

    target_contracts = 1
    payload = build_signal_post_payload(
        signal=signal,
        session_date=session_date,
        equity=equity,
        target_contracts=target_contracts,
        strategy_version=strategy_version,
        emitted_at_utc=emitted_at_utc,
    )

    if dry_run:
        log.info(
            "trigger_v1_cycle_dry_run_would_post",
            market=signal.market,
            direction=signal.direction.value,
            target_contracts=target_contracts,
            decision_price=str(signal.decision_price),
            stop_price=str(signal.stop_price),
            session_date=session_date.isoformat(),
            payload_keys=sorted(payload.keys()),
        )
        return SignalPostOutcome(market=signal.market, status="dry_run_skipped")

    # mypy narrowing — dry_run=False means the caller wired both http
    # + bearer; the main entry point asserts these before getting here.
    assert http_client is not None
    assert bearer_token is not None

    try:
        status_code, body = await post_signal(
            http_client=http_client,
            api_base_url=api_base_url,
            bearer_token=bearer_token,
            payload=payload,
        )
    except httpx.HTTPError as exc:
        log.error(
            "trigger_v1_cycle_post_network_failed",
            market=signal.market,
            error=str(exc),
            error_class=type(exc).__name__,
        )
        return SignalPostOutcome(
            market=signal.market,
            status="post_failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    if 200 <= status_code < 300:
        signal_id = body.get("signal_id") if isinstance(body, dict) else None
        audit_event_uuid = body.get("audit_event_uuid") if isinstance(body, dict) else None
        log.info(
            "trigger_v1_cycle_post_succeeded",
            market=signal.market,
            direction=signal.direction.value,
            target_contracts=target_contracts,
            decision_price=str(signal.decision_price),
            session_date=session_date.isoformat(),
            http_status_code=status_code,
            signal_id=signal_id,
            audit_event_uuid=audit_event_uuid,
        )
        return SignalPostOutcome(
            market=signal.market,
            status="posted",
            http_status_code=status_code,
            signal_id=str(signal_id) if signal_id else None,
            audit_event_uuid=str(audit_event_uuid) if audit_event_uuid else None,
        )

    log.error(
        "trigger_v1_cycle_post_rejected",
        market=signal.market,
        http_status_code=status_code,
        api_error_body=body,
    )
    return SignalPostOutcome(
        market=signal.market,
        status="post_failed",
        http_status_code=status_code,
        error=f"api returned {status_code}: {body}",
    )


# ---------------------------------------------------------------------------
# Tick orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CycleSummary:
    """Aggregate outcome surfaced to the operator + the exit-code computer."""

    signals_generated: int
    signals_emitted: int
    dedup_skipped: int
    dry_run_skipped: int
    post_failed: int
    rejections_count: int
    rejection_reasons: dict[str, int]
    history_missing_markets: tuple[str, ...]
    stale_markets: tuple[str, ...]


def _summarize(
    *,
    outcomes: Sequence[SignalPostOutcome],
    rejections: Sequence[tuple[str, Any]],
    history_missing_markets: tuple[str, ...],
    stale_markets: tuple[str, ...],
) -> CycleSummary:
    """Roll per-signal outcomes + strategy rejections into a single summary."""
    reason_counts: dict[str, int] = {}
    for _, reason in rejections:
        reason_str = reason.value if hasattr(reason, "value") else str(reason)
        reason_counts[reason_str] = reason_counts.get(reason_str, 0) + 1
    return CycleSummary(
        signals_generated=len(outcomes),
        signals_emitted=sum(1 for o in outcomes if o.status == "posted"),
        dedup_skipped=sum(1 for o in outcomes if o.status == "dedup_skipped"),
        dry_run_skipped=sum(1 for o in outcomes if o.status == "dry_run_skipped"),
        post_failed=sum(1 for o in outcomes if o.status == "post_failed"),
        rejections_count=len(rejections),
        rejection_reasons=reason_counts,
        history_missing_markets=history_missing_markets,
        stale_markets=stale_markets,
    )


async def run_cycle(
    *,
    args: ParsedArgs,
    context: CycleContext,
    http_client: httpx.AsyncClient | None,
    bearer_token: str | None,
) -> CycleSummary:
    """Build BarSeries + run strategy + emit each signal with dedup + dry-run gates.

    Returns the aggregate summary; exit-code computation lives in
    ``_compute_exit_code``.
    """
    active_universe_markets = compute_active_universe()
    strategy = V1TrendFollowing(parameters=context.parameters)

    active_universe: dict[str, BarSeries] = {}
    history_missing: list[str] = []
    stale_markets: list[str] = []
    for market in active_universe_markets:
        bars = load_bars_for_market(market, args.data_root, as_of=args.session_date)
        if not bars:
            history_missing.append(market)
            continue
        if is_bar_series_stale(bars=bars, as_of=args.session_date):
            stale_markets.append(market)
            log.warning(
                "trigger_v1_cycle_bars_stale",
                market=market,
                newest_session_date=bars[-1].session_date.isoformat(),
                as_of_session_date=args.session_date.isoformat(),
                threshold_days=BAR_STALENESS_WARN_DAYS,
                action="continuing (operator-trigger may want last-known bars)",
            )
        try:
            active_universe[market] = BarSeries(market=market, bars=tuple(bars))
        except ValueError as exc:
            log.error(
                "trigger_v1_cycle_barseries_invalid",
                market=market,
                error=str(exc),
            )
            history_missing.append(market)
            continue

    if history_missing:
        log.warning(
            "trigger_v1_cycle_history_missing",
            missing_markets=history_missing,
            note=(
                "These markets have no usable bars on disk; strategy "
                "will skip them. Investigate bar_sync if any unexpected."
            ),
        )

    result = strategy.generate_signals(
        active_universe=active_universe,
        current_positions=context.current_positions,
        as_of_session_date=args.session_date,
    )

    # Per-rejection log lines for forensic visibility (matches the
    # LEAN cycle's pattern at lean/v1_strategy.py:503-511).
    for rejected_market, reason in result.rejections:
        reason_str = reason.value if hasattr(reason, "value") else str(reason)
        log.info(
            "trigger_v1_cycle_rejection",
            market=rejected_market,
            reason=reason_str,
            session_date=args.session_date.isoformat(),
        )

    emitted_at_utc = datetime.now(tz=UTC)
    outcomes: list[SignalPostOutcome] = []
    for signal in result.signals:
        outcome = await emit_signal_with_dedup(
            signal=signal,
            session_date=args.session_date,
            equity=context.latest_balance_nav,
            already_emitted=context.already_emitted_markets,
            strategy_version=STRATEGY_VERSION_MARKER,
            emitted_at_utc=emitted_at_utc,
            dry_run=args.dry_run,
            http_client=http_client,
            api_base_url=args.api_base_url,
            bearer_token=bearer_token,
        )
        outcomes.append(outcome)

    return _summarize(
        outcomes=outcomes,
        rejections=result.rejections,
        history_missing_markets=tuple(history_missing),
        stale_markets=tuple(stale_markets),
    )


# ---------------------------------------------------------------------------
# Exit-code computation
# ---------------------------------------------------------------------------


def _compute_exit_code(summary: CycleSummary, dry_run: bool) -> int:
    """Aggregate cycle summary into a single process exit code.

    Precedence: post_failed > OK. Stale markets / history_missing do
    NOT bubble up to exit code — they're observability conditions
    that show up in the structured log + the summary line.

    In dry-run mode post_failed is always 0 (no POSTs attempted) so
    we always return OK.
    """
    if dry_run:
        return EXIT_OK
    if summary.post_failed > 0:
        return EXIT_POST_FAILED
    return EXIT_OK


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


async def _build_session_factory(database_url: str) -> tuple[Any, Any]:
    """Build a one-shot SQLAlchemy async engine + sessionmaker.

    Mirrors the verify_chain / replay_executions / recovery_agent
    one-shot pattern — we do NOT reuse services.api.db's pool because
    the script runs independently of the api lifespan.
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


async def _amain(
    args: ParsedArgs,
    *,
    session_factory_override: Any = None,
    http_client_override: httpx.AsyncClient | None = None,
    bearer_token_override: str | None = None,
) -> int:
    """Async entry point. Returns process exit code.

    Test hooks via the three overrides:

    * ``session_factory_override`` — skip the engine creation step
    * ``http_client_override`` — fake the httpx client for POSTs
    * ``bearer_token_override`` — fake the LEAN bearer token (default
      reads from LEAN_LOCAL_BEARER_TOKEN env var)
    """
    log.info(
        "trigger_v1_cycle_started",
        env=args.env,
        session_date=args.session_date.isoformat(),
        dry_run=args.dry_run,
        data_root=str(args.data_root),
        api_base_url=args.api_base_url,
    )

    # ---- bearer token check (only when actually POSTing) -------------
    bearer_token: str | None = bearer_token_override
    if not args.dry_run and bearer_token is None:
        bearer_token = os.environ.get(LEAN_BEARER_ENV)
        if not bearer_token:
            log.error(
                "trigger_v1_cycle_bearer_missing",
                reason=(
                    f"{LEAN_BEARER_ENV} env var is unset; cannot POST "
                    "without authenticating to /api/internal/lean/signals. "
                    "Stage via sops decrypt per scripts/operator_tools/README.md."
                ),
            )
            return EXIT_BEARER_MISSING

    # ---- DB session factory ------------------------------------------
    engine: Any = None
    session_factory: Any = session_factory_override
    if session_factory is None:
        database_url = os.environ.get(DATABASE_URL_ENV)
        if not database_url:
            log.error(
                "trigger_v1_cycle_db_init_failed",
                reason=f"{DATABASE_URL_ENV} is not set",
            )
            return EXIT_DB_INIT_FAILED
        try:
            engine, session_factory = await _build_session_factory(database_url)
        except Exception as exc:
            log.error(
                "trigger_v1_cycle_db_init_failed",
                error=str(exc),
                error_class=type(exc).__name__,
            )
            return EXIT_DB_INIT_FAILED

    # ---- Load cycle context ------------------------------------------
    active_universe = compute_active_universe()
    try:
        context = await load_cycle_context(
            session_factory,
            env=args.env,
            session_date=args.session_date,
            active_universe=active_universe,
        )
    except Exception as exc:
        log.error(
            "trigger_v1_cycle_context_load_failed",
            error=str(exc),
            error_class=type(exc).__name__,
        )
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as dispose_exc:
                log.warning("trigger_v1_cycle_db_dispose_error", error=str(dispose_exc))
        return EXIT_UNEXPECTED

    if context is None:
        log.error(
            "trigger_v1_cycle_no_active_account",
            note=(
                "accounts table has no active row. Run the accounts "
                "bootstrap migration before invoking this tool."
            ),
        )
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as dispose_exc:
                log.warning("trigger_v1_cycle_db_dispose_error", error=str(dispose_exc))
        return EXIT_DB_INIT_FAILED

    log.info(
        "trigger_v1_cycle_context_loaded",
        account_id=str(context.account_id),
        risk_state=context.risk_state,
        latest_balance_nav=str(context.latest_balance_nav),
        active_universe=list(active_universe),
        sidelined_markets=sorted(V1_SIDELINED_MARKETS),
        positions_loaded=len(context.current_positions),
        already_emitted_markets=sorted(context.already_emitted_markets),
    )

    # ---- Risk-state gate ---------------------------------------------
    if context.risk_state != "NORMAL":
        log.error(
            "trigger_v1_cycle_risk_state_blocked",
            risk_state=context.risk_state,
            note=(
                "Cycle aborted because risk_state is not NORMAL. "
                "Operator-trigger is not a kill-switch backdoor. "
                "Resume from HALT_NEW manually via the web UI."
            ),
        )
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as dispose_exc:
                log.warning("trigger_v1_cycle_db_dispose_error", error=str(dispose_exc))
        return EXIT_RISK_STATE_BLOCKED

    # ---- Run cycle ---------------------------------------------------
    http_client = (
        http_client_override
        if http_client_override is not None
        else httpx.AsyncClient(timeout=httpx.Timeout(args.http_timeout_seconds))
    )
    try:
        summary = await run_cycle(
            args=args,
            context=context,
            http_client=http_client,
            bearer_token=bearer_token,
        )
    except Exception as exc:
        log.error(
            "trigger_v1_cycle_run_failed",
            error=str(exc),
            error_class=type(exc).__name__,
            traceback=traceback.format_exc(),
        )
        return EXIT_UNEXPECTED
    finally:
        if http_client_override is None:
            try:
                await http_client.aclose()
            except Exception as exc:
                log.warning("trigger_v1_cycle_http_client_close_error", error=str(exc))
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log.warning("trigger_v1_cycle_db_dispose_error", error=str(exc))

    log.info(
        "trigger_v1_cycle_completed",
        session_date=args.session_date.isoformat(),
        signals_generated=summary.signals_generated,
        signals_emitted=summary.signals_emitted,
        dedup_skipped=summary.dedup_skipped,
        dry_run_skipped=summary.dry_run_skipped,
        post_failed=summary.post_failed,
        rejections_count=summary.rejections_count,
        rejection_reasons=summary.rejection_reasons,
        history_missing_markets=list(summary.history_missing_markets),
        stale_markets=list(summary.stale_markets),
        dry_run=args.dry_run,
    )
    return _compute_exit_code(summary, args.dry_run)


def main(argv: Sequence[str] | None = None) -> int:
    """Sync entry point. Returns exit code; never sys.exits."""
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
    "API_URL_ENV",
    "BAR_STALENESS_WARN_DAYS",
    "DATABASE_URL_ENV",
    "DATA_ROOT_ENV",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_FUTURES_UNIVERSE_LOOKBACK_DAYS",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "EXIT_BAD_ARGS",
    "EXIT_BEARER_MISSING",
    "EXIT_DB_INIT_FAILED",
    "EXIT_OK",
    "EXIT_POST_FAILED",
    "EXIT_RISK_STATE_BLOCKED",
    "EXIT_UNEXPECTED",
    "LEAN_BEARER_ENV",
    "STRATEGY_VERSION_MARKER",
    "CycleContext",
    "CycleSummary",
    "ParsedArgs",
    "SignalPostOutcome",
    "_amain",
    "_build_parser",
    "_compute_exit_code",
    "_parse_args",
    "_summarize",
    "build_minimal_sizing_trace",
    "build_position_from_row",
    "build_signal_post_payload",
    "build_v1_parameters_from_dict",
    "compute_active_universe",
    "emit_signal_with_dedup",
    "fetch_account_id",
    "fetch_active_parameters_dict",
    "fetch_already_emitted_markets",
    "fetch_latest_balance_nav",
    "fetch_positions_for_v1_markets",
    "fetch_risk_state_for_account",
    "is_bar_series_stale",
    "load_bars_for_market",
    "load_cycle_context",
    "main",
    "post_signal",
    "read_etf_bars_from_zip",
    "read_futures_bars_from_universe_files",
    "run_cycle",
]
