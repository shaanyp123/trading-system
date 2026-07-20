"""Reconcile an operator-supplied CFM statement export against the backend's books.

Strategy §10 gate **A1** requires modeled P&L to match CFM statement P&L
within **±$1 or ±2% per trade** (whichever is larger). This tool is the
mechanical form of the manual reconciliation performed in the
2026-07-18 session (decisions-log "P&L verification CLOSED"): every
venue cash movement is classified — trade round trips, fees, funding
settlements, capital events (deposits / withdrawals / conversions),
subscription charges (the annual Coinbase One debit that tripped the
Jul-17 floor), reward credits — and per-trade P&L is checked against
``trades.realized_pnl_usd`` at A1 tolerance.

READ-ONLY: no DB writes, no audit events, no venue calls — it reads the
backend tables and a CSV the operator downloads from Coinbase. Because
it writes nothing, there is no ``--dry-run``/``--confirm`` gate.

Statement format ([A27] honesty): the exact CFM statement CSV layout is
venue-controlled and will be calibrated on first live use. The parser
therefore takes explicit column names (``--col-time``, ``--col-type``,
``--col-amount``, ``--col-description``) with generic defaults, and any
unclassifiable line lands in the UNATTRIBUTED bucket loudly rather than
being guessed at. First-use checklist lives in
``scripts/operator_tools/README.md``.

Classification rules (case-insensitive substring match on type +
description, modeled on the venue lines observed 2026-07-14 → 2026-07-18):

* ``funding``                          → funding settlement
* ``coinbase one`` / ``subscription``  → subscription charge
* ``deposit`` / ``withdrawal`` / ``transfer`` / ``conversion`` / ``convert``
  / ``sweep``                          → capital / cash movement
* ``reward``                           → USDC reward credit
* ``fee`` / ``commission``             → standalone fee line
* ``futures`` / ``trade`` / ``pnl`` / ``settlement``
                                       → trade P&L line (A1-checked)
* anything else                        → UNATTRIBUTED (listed verbatim)

Sweep awareness (pre-C2 item, deploy/cash_manager/README.md): cash-yield
sweeps are INTERNAL transfers — the §3.6 cash manager converts USD↔USDC
and (for reclaims) schedules a futures sweep, and each completed sweep
is ledgered in ``cash_sweeps``. Statement lines for those movements must
NOT read as capital events (they are not deposits/withdrawals of
capital). After keyword classification, any conversion/transfer/sweep-
shaped ``capital_event`` line whose |amount| equals a completed
``cash_sweeps.amount_usd`` completed within ±1 UTC day is reclassified
``sweep`` (a ``to_margin`` reclaim may legitimately produce TWO such
lines — convert + futures sweep — so one sweep row can claim several
lines). ``deposit``/``withdrawal``-keyword lines are deliberately NOT
eligible — a genuine capital movement that happens to equal a sweep
amount must stay in the capital bucket. A completed sweep with NO
matching line inside the statement's own date coverage is reported
loudly and flips the verdict to REVIEW (the ledger says money moved;
the statement doesn't show it). With zero ``cash_sweeps`` rows — the
dormant-cash-manager reality — classification is bit-identical to the
pre-sweep-aware behavior.

Usage::

    DATABASE_URL=postgresql+asyncpg://... \
    python -m scripts.operator_tools.reconcile_statement \
        --statement /path/to/cfm_statement.csv \
        --since 2026-07-09

Exit codes: 0 = all matched trades within A1 tolerance, nothing
unattributed, and no completed sweep missing its statement line; 2 = at
least one tolerance failure, unattributed line, or unmatched completed
sweep (report printed either way); 4 = no active account; 5 = DB init
failed; 6 = bad arguments; 99 = unexpected error.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = structlog.get_logger()

EXIT_OK: Final[int] = 0
EXIT_MISMATCH: Final[int] = 2
EXIT_NO_ACCOUNT: Final[int] = 4
EXIT_DB_INIT: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99

DATABASE_URL_ENV: Final[str] = "DATABASE_URL"

#: A1 tolerance: ±$1 or ±2% of the modeled value, whichever is larger.
A1_ABS_TOLERANCE_USD: Final[Decimal] = Decimal("1")
A1_REL_TOLERANCE: Final[Decimal] = Decimal("0.02")

LineClass = Literal[
    "trade_pnl",
    "fee",
    "funding",
    "capital_event",
    "sweep",
    "subscription",
    "reward",
    "unattributed",
]

#: Ordered (class, substrings) rules — first match wins. Funding before
#: trade so "funding settlement" never lands in the trade bucket, and
#: subscription before fee so "Coinbase One fee" stays a subscription.
#: ``sweep`` sits in the capital rules so a "futures sweep" line never
#: falls through to the ``futures`` needle and pollutes the A1 trade
#: bucket; cross-referencing against ``cash_sweeps`` then decides
#: capital_event vs sweep.
_CLASS_RULES: Final[tuple[tuple[LineClass, tuple[str, ...]], ...]] = (
    ("funding", ("funding",)),
    ("subscription", ("coinbase one", "subscription")),
    ("reward", ("reward",)),
    (
        "capital_event",
        ("deposit", "withdrawal", "transfer", "conversion", "convert", "sweep"),
    ),
    ("fee", ("fee", "commission")),
    ("trade_pnl", ("futures", "trade", "pnl", "settlement")),
)

#: Substrings marking a capital_event line as sweep-SHAPED (eligible for
#: ``cash_sweeps`` cross-reference). Deliberately excludes ``deposit`` /
#: ``withdrawal`` — genuine capital movements must never be reclassified
#: away from the capital bucket on an amount coincidence.
_SWEEP_SHAPED_NEEDLES: Final[tuple[str, ...]] = (
    "transfer",
    "conversion",
    "convert",
    "sweep",
)

#: A statement line and a completed sweep match when the sweep completed
#: within this many days of the line's UTC date (venue settlement /
#: statement-timestamp skew margin; sweeps are same-day by design).
_SWEEP_MATCH_DAY_SLACK: Final[int] = 1


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One parsed statement row."""

    occurred_at: datetime | None
    raw_type: str
    description: str
    amount_usd: Decimal
    line_class: LineClass


@dataclass(frozen=True, slots=True)
class DbTrade:
    """A closed ``trades`` row (the modeled side of the A1 check)."""

    trade_id: UUID
    market: str
    direction: str
    closed_at_utc: datetime
    realized_pnl_usd: Decimal


@dataclass(frozen=True, slots=True)
class TradeMatch:
    """One modeled trade vs its best statement candidate."""

    trade: DbTrade
    statement_amount: Decimal | None
    difference: Decimal | None
    tolerance: Decimal
    passed: bool


@dataclass(frozen=True, slots=True)
class DbCashSweep:
    """A completed ``cash_sweeps`` row (the ledger side of sweep matching)."""

    sweep_id: int
    direction: str
    amount_usd: Decimal
    completed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class SweepMatch:
    """One statement line reclassified ``capital_event`` → ``sweep``."""

    line: StatementLine
    sweep: DbCashSweep


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """The full reconciliation output (pure; rendered by ``_render``)."""

    matches: tuple[TradeMatch, ...]
    class_totals: dict[LineClass, Decimal]
    class_counts: dict[LineClass, int]
    unattributed: tuple[StatementLine, ...]
    db_realized_pnl_total: Decimal
    statement_net_total: Decimal
    sweep_matches: tuple[SweepMatch, ...] = ()
    unmatched_sweeps: tuple[DbCashSweep, ...] = ()

    @property
    def all_passed(self) -> bool:
        return (
            all(m.passed for m in self.matches)
            and not self.unattributed
            and not self.unmatched_sweeps
        )


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    statement_path: Path
    since: date | None
    until: date | None
    col_time: str
    col_type: str
    col_amount: str
    col_description: str


# ---------------------------------------------------------------------------
# Pure policy — parsing, classification, matching
# ---------------------------------------------------------------------------


def classify_line(raw_type: str, description: str) -> LineClass:
    """First-match classification over type + description (lowercased)."""
    haystack = f"{raw_type} {description}".lower()
    for line_class, needles in _CLASS_RULES:
        if any(needle in haystack for needle in needles):
            return line_class
    return "unattributed"


def _is_sweep_shaped(line: StatementLine) -> bool:
    haystack = f"{line.raw_type} {line.description}".lower()
    return any(needle in haystack for needle in _SWEEP_SHAPED_NEEDLES)


def reclassify_sweep_lines(
    lines: list[StatementLine],
    sweeps: list[DbCashSweep],
) -> tuple[list[StatementLine], tuple[SweepMatch, ...], tuple[DbCashSweep, ...]]:
    """Cross-reference capital lines against completed sweeps (pure).

    Returns ``(lines, sweep_matches, unmatched_sweeps)`` where matched
    lines have been reclassified ``capital_event`` → ``sweep``.

    Matching rule — a line matches a sweep when ALL of:

    * the line classified ``capital_event`` AND is sweep-shaped
      (conversion/transfer/sweep keywords; NOT deposit/withdrawal)
    * the line has a timestamp (a dateless line stays capital_event —
      conservative: never reclassify on amount alone)
    * ``|line.amount_usd| == sweep.amount_usd`` (Decimal equality, so
      trailing zeros don't matter)
    * the line's UTC date is within ±``_SWEEP_MATCH_DAY_SLACK`` days of
      the sweep's ``completed_at_utc`` date

    One sweep may claim MULTIPLE lines (a ``to_margin`` reclaim produces
    a convert line + a futures-sweep line, both for ``amount_usd``);
    each line claims exactly one sweep (nearest completion date, sweep
    id as the deterministic tiebreak).

    ``unmatched_sweeps`` is limited to sweeps completed inside the
    statement's own observed date coverage (±slack) — a sweep outside
    the window the operator exported cannot be expected in the CSV. With
    no dated lines at all, no sweep is flagged (nothing to anchor
    coverage to).

    With ``sweeps == []`` the output is the input — bit-identical to the
    sweep-blind behavior (test-pinned).
    """
    if not sweeps:
        return list(lines), (), ()

    out_lines: list[StatementLine] = []
    matches: list[SweepMatch] = []
    matched_sweep_ids: set[int] = set()
    for line in lines:
        if (
            line.line_class != "capital_event"
            or line.occurred_at is None
            or not _is_sweep_shaped(line)
        ):
            out_lines.append(line)
            continue
        line_day = line.occurred_at.astimezone(UTC).date()
        best: DbCashSweep | None = None
        best_rank: tuple[int, int] | None = None
        for sweep in sweeps:
            if abs(line.amount_usd) != sweep.amount_usd:
                continue
            day_delta = abs((sweep.completed_at_utc.astimezone(UTC).date() - line_day).days)
            if day_delta > _SWEEP_MATCH_DAY_SLACK:
                continue
            rank = (day_delta, sweep.sweep_id)
            if best_rank is None or rank < best_rank:
                best = sweep
                best_rank = rank
        if best is None:
            out_lines.append(line)
            continue
        reclassified = StatementLine(
            occurred_at=line.occurred_at,
            raw_type=line.raw_type,
            description=line.description,
            amount_usd=line.amount_usd,
            line_class="sweep",
        )
        out_lines.append(reclassified)
        matches.append(SweepMatch(line=reclassified, sweep=best))
        matched_sweep_ids.add(best.sweep_id)

    dated_days = [
        line.occurred_at.astimezone(UTC).date() for line in lines if line.occurred_at is not None
    ]
    unmatched: list[DbCashSweep] = []
    if dated_days:
        coverage_start = min(dated_days)
        coverage_end = max(dated_days)
        for sweep in sweeps:
            if sweep.sweep_id in matched_sweep_ids:
                continue
            sweep_day = sweep.completed_at_utc.astimezone(UTC).date()
            inside = (coverage_start - sweep_day).days <= _SWEEP_MATCH_DAY_SLACK and (
                sweep_day - coverage_end
            ).days <= _SWEEP_MATCH_DAY_SLACK
            if inside:
                unmatched.append(sweep)
    return out_lines, tuple(matches), tuple(unmatched)


def _parse_amount(raw: str) -> Decimal | None:
    cleaned = raw.strip().replace("$", "").replace(",", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"  # accounting-negative style
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _parse_time(raw: str) -> datetime | None:
    cleaned = raw.strip().replace("Z", "+00:00")
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_statement_rows(
    rows: list[dict[str, str]],
    *,
    args: ParsedArgs,
) -> tuple[list[StatementLine], list[dict[str, str]]]:
    """CSV dict-rows → classified lines + rows the parser could not read.

    A row without a parseable amount is returned in the rejects list —
    surfaced verbatim, never silently dropped.
    """
    lines: list[StatementLine] = []
    rejects: list[dict[str, str]] = []
    for row in rows:
        amount = _parse_amount(row.get(args.col_amount, "") or "")
        if amount is None:
            rejects.append(row)
            continue
        occurred_at = _parse_time(row.get(args.col_time, "") or "")
        if occurred_at is not None:
            day = occurred_at.astimezone(UTC).date()
            if args.since is not None and day < args.since:
                continue
            if args.until is not None and day > args.until:
                continue
        raw_type = (row.get(args.col_type, "") or "").strip()
        description = (row.get(args.col_description, "") or "").strip()
        lines.append(
            StatementLine(
                occurred_at=occurred_at,
                raw_type=raw_type,
                description=description,
                amount_usd=amount,
                line_class=classify_line(raw_type, description),
            )
        )
    return lines, rejects


def a1_tolerance(modeled: Decimal) -> Decimal:
    """±$1 or ±2% of the modeled value, whichever is larger (§10 A1)."""
    return max(A1_ABS_TOLERANCE_USD, (abs(modeled) * A1_REL_TOLERANCE))


def match_trades(
    trades: list[DbTrade],
    lines: list[StatementLine],
) -> tuple[TradeMatch, ...]:
    """Greedy nearest-amount match of trade_pnl lines to closed trades.

    Trades are processed in close order; each consumes the unclaimed
    trade_pnl line with the smallest |difference| (same-day lines
    preferred when timestamps exist). A trade with no candidate fails
    with ``statement_amount=None`` — an absent line is a mismatch, not a
    skip.
    """
    unclaimed = list(lines)
    matches: list[TradeMatch] = []
    for trade in sorted(trades, key=lambda t: t.closed_at_utc):
        tolerance = a1_tolerance(trade.realized_pnl_usd)
        best_index: int | None = None
        best_rank: tuple[Decimal, int] | None = None
        for index, line in enumerate(unclaimed):
            diff = abs(line.amount_usd - trade.realized_pnl_usd)
            same_day = (
                line.occurred_at is not None
                and line.occurred_at.astimezone(UTC).date()
                == trade.closed_at_utc.astimezone(UTC).date()
            )
            # Smallest |diff| wins; same-day breaks ties.
            rank = (diff, 0 if same_day else 1)
            if best_rank is None or rank < best_rank:
                best_index = index
                best_rank = rank
        if best_index is None:
            matches.append(
                TradeMatch(
                    trade=trade,
                    statement_amount=None,
                    difference=None,
                    tolerance=tolerance,
                    passed=False,
                )
            )
            continue
        line = unclaimed.pop(best_index)
        difference = line.amount_usd - trade.realized_pnl_usd
        matches.append(
            TradeMatch(
                trade=trade,
                statement_amount=line.amount_usd,
                difference=difference,
                tolerance=tolerance,
                passed=abs(difference) <= tolerance,
            )
        )
    return tuple(matches)


def build_report(
    trades: list[DbTrade],
    lines: list[StatementLine],
    sweeps: list[DbCashSweep] | None = None,
) -> ReconcileReport:
    """Assemble the full reconciliation (pure)."""
    lines, sweep_matches, unmatched_sweeps = reclassify_sweep_lines(lines, sweeps or [])
    class_totals: dict[LineClass, Decimal] = {}
    class_counts: dict[LineClass, int] = {}
    for line in lines:
        class_totals[line.line_class] = (
            class_totals.get(line.line_class, Decimal(0)) + line.amount_usd
        )
        class_counts[line.line_class] = class_counts.get(line.line_class, 0) + 1
    trade_lines = [line for line in lines if line.line_class == "trade_pnl"]
    return ReconcileReport(
        matches=match_trades(trades, trade_lines),
        class_totals=class_totals,
        class_counts=class_counts,
        unattributed=tuple(line for line in lines if line.line_class == "unattributed"),
        db_realized_pnl_total=sum((t.realized_pnl_usd for t in trades), start=Decimal(0)),
        statement_net_total=sum((line.amount_usd for line in lines), start=Decimal(0)),
        sweep_matches=sweep_matches,
        unmatched_sweeps=unmatched_sweeps,
    )


def _render(report: ReconcileReport, rejects: list[dict[str, str]]) -> str:
    out: list[str] = ["", "=== CFM statement reconciliation (§10 A1) ==="]
    out.append("")
    out.append("Per-trade A1 check (±$1 or ±2%, whichever larger):")
    if not report.matches:
        out.append("  (no closed trades in window)")
    for m in report.matches:
        stmt = f"{m.statement_amount}" if m.statement_amount is not None else "NO LINE"
        diff = f"{m.difference}" if m.difference is not None else "n/a"
        verdict = "PASS" if m.passed else "FAIL"
        out.append(
            f"  [{verdict}] {m.trade.market} {m.trade.direction} closed "
            f"{m.trade.closed_at_utc.date().isoformat()} · modeled "
            f"{m.trade.realized_pnl_usd} vs statement {stmt} "
            f"(diff {diff}, tol ±{m.tolerance})"
        )
    out.append("")
    out.append("Statement lines by class:")
    for line_class in sorted(report.class_totals):
        out.append(
            f"  {line_class:<14} {report.class_counts[line_class]:>4} lines · "
            f"net {report.class_totals[line_class]}"
        )
    out.append("")
    out.append(f"DB modeled realized P&L total: {report.db_realized_pnl_total}")
    out.append(f"Statement net total:           {report.statement_net_total}")
    if report.sweep_matches:
        out.append("")
        out.append("Sweep-classified lines (cross-referenced against cash_sweeps):")
        for sm in report.sweep_matches:
            when = sm.line.occurred_at.isoformat() if sm.line.occurred_at else "?"
            out.append(
                f"  {when} · {sm.line.raw_type} · {sm.line.description} · "
                f"{sm.line.amount_usd} → sweep id={sm.sweep.sweep_id} "
                f"({sm.sweep.direction} {sm.sweep.amount_usd}, completed "
                f"{sm.sweep.completed_at_utc.date().isoformat()})"
            )
    if report.unmatched_sweeps:
        out.append("")
        out.append(
            "COMPLETED SWEEPS WITH NO STATEMENT LINE (ledger says money "
            "moved; the statement doesn't show it — investigate):"
        )
        for sweep in report.unmatched_sweeps:
            out.append(
                f"  sweep id={sweep.sweep_id} · {sweep.direction} · "
                f"{sweep.amount_usd} · completed "
                f"{sweep.completed_at_utc.date().isoformat()}"
            )
    if report.unattributed:
        out.append("")
        out.append("UNATTRIBUTED lines (classify manually — the Jul-17 lesson):")
        for line in report.unattributed:
            when = line.occurred_at.isoformat() if line.occurred_at else "?"
            out.append(f"  {when} · {line.raw_type} · {line.description} · {line.amount_usd}")
    if rejects:
        out.append("")
        out.append(f"UNPARSEABLE rows (no amount): {len(rejects)} — check column flags")
    out.append("")
    out.append("RESULT: " + ("CLEAN" if report.all_passed and not rejects else "REVIEW NEEDED"))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


async def _load_trades(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: date | None,
    until: date | None,
) -> tuple[list[DbTrade] | None, int]:
    """Closed trades for the active account (None ⇒ no active account)."""
    async with session_factory() as session:
        account_row = (
            await session.execute(
                text(
                    "SELECT id FROM accounts WHERE active_to IS NULL "
                    "ORDER BY active_from DESC LIMIT 1"
                )
            )
        ).fetchone()
        if account_row is None:
            return None, EXIT_NO_ACCOUNT
        rows = (
            await session.execute(
                text(
                    "SELECT id, market, direction, closed_at_utc, realized_pnl_usd "
                    "FROM trades "
                    "WHERE account_id = :acct AND closed_at_utc IS NOT NULL "
                    "  AND realized_pnl_usd IS NOT NULL "
                    "  AND (CAST(:since AS DATE) IS NULL OR closed_at_utc >= :since) "
                    "  AND (CAST(:until AS DATE) IS NULL "
                    "       OR closed_at_utc < CAST(:until AS DATE) + INTERVAL '1 day') "
                    "ORDER BY closed_at_utc"
                ),
                {"acct": account_row.id, "since": since, "until": until},
            )
        ).fetchall()
    trades = [
        DbTrade(
            trade_id=row.id,
            market=str(row.market),
            direction=str(row.direction),
            closed_at_utc=row.closed_at_utc,
            realized_pnl_usd=Decimal(str(row.realized_pnl_usd)),
        )
        for row in rows
    ]
    return trades, EXIT_OK


async def _load_cash_sweeps(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: date | None,
    until: date | None,
) -> list[DbCashSweep]:
    """Completed sweeps overlapping the window (± the matching slack).

    ``cash_sweeps`` is venue-scoped, not account-scoped (2026-07-08
    migration rationale), so no account filter. The window is widened by
    the day slack on both sides so an edge-of-window statement line can
    still find its sweep.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, direction, amount_usd, completed_at_utc "
                    "FROM cash_sweeps "
                    "WHERE status = 'completed' AND completed_at_utc IS NOT NULL "
                    "  AND (CAST(:since AS DATE) IS NULL "
                    "       OR completed_at_utc >= "
                    "          CAST(:since AS DATE) - CAST(:slack_days || ' days' AS INTERVAL)) "
                    "  AND (CAST(:until AS DATE) IS NULL "
                    "       OR completed_at_utc < CAST(:until AS DATE) + INTERVAL '1 day' "
                    "          + CAST(:slack_days || ' days' AS INTERVAL)) "
                    "ORDER BY completed_at_utc"
                ),
                {"since": since, "until": until, "slack_days": _SWEEP_MATCH_DAY_SLACK},
            )
        ).fetchall()
    return [
        DbCashSweep(
            sweep_id=int(row.id),
            direction=str(row.direction),
            amount_usd=Decimal(str(row.amount_usd)),
            completed_at_utc=row.completed_at_utc,
        )
        for row in rows
    ]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile_statement",
        description="Reconcile a CFM statement CSV against backend trades (read-only).",
    )
    parser.add_argument("--statement", required=True, help="Path to the statement CSV export.")
    parser.add_argument("--since", default=None, help="Earliest UTC date (YYYY-MM-DD).")
    parser.add_argument("--until", default=None, help="Latest UTC date (YYYY-MM-DD).")
    parser.add_argument("--col-time", default="time", help="CSV column: timestamp.")
    parser.add_argument("--col-type", default="type", help="CSV column: line type.")
    parser.add_argument("--col-amount", default="amount", help="CSV column: USD amount.")
    parser.add_argument("--col-description", default="description", help="CSV column: description.")
    return parser


def parse_args(argv: list[str] | None = None) -> ParsedArgs:
    ns = _build_parser().parse_args(argv)
    try:
        since = date.fromisoformat(ns.since) if ns.since else None
        until = date.fromisoformat(ns.until) if ns.until else None
    except ValueError as exc:
        raise ValueError(f"bad --since/--until date: {exc}") from exc
    return ParsedArgs(
        statement_path=Path(ns.statement),
        since=since,
        until=until,
        col_time=ns.col_time,
        col_type=ns.col_type,
        col_amount=ns.col_amount,
        col_description=ns.col_description,
    )


async def _amain(args: ParsedArgs) -> int:
    if not args.statement_path.is_file():
        log.error("reconcile_statement_missing_file", path=str(args.statement_path))
        return EXIT_BAD_ARGS
    with args.statement_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    lines, rejects = parse_statement_rows(rows, args=args)

    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        log.error("reconcile_statement_no_database_url", env_var=DATABASE_URL_ENV)
        return EXIT_DB_INIT
    try:
        engine = create_async_engine(url, future=True)
    except Exception:
        log.error("reconcile_statement_engine_init_failed")
        return EXIT_DB_INIT
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        trades, code = await _load_trades(session_factory, since=args.since, until=args.until)
        if trades is None:
            log.error("reconcile_statement_no_active_account")
            return code
        sweeps = await _load_cash_sweeps(session_factory, since=args.since, until=args.until)
    finally:
        await engine.dispose()

    report = build_report(trades, lines, sweeps)
    sys.stdout.write(_render(report, rejects) + "\n")
    log.info(
        "reconcile_statement_completed",
        trades=len(report.matches),
        passed=sum(1 for m in report.matches if m.passed),
        sweep_lines=len(report.sweep_matches),
        unmatched_sweeps=len(report.unmatched_sweeps),
        unattributed=len(report.unattributed),
        unparseable=len(rejects),
    )
    return EXIT_OK if report.all_passed and not rejects else EXIT_MISMATCH


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            code = exc.code if isinstance(exc.code, int) else EXIT_BAD_ARGS
            return code if code in (0, EXIT_BAD_ARGS) else EXIT_BAD_ARGS
        log.error("reconcile_statement_bad_args", error=str(exc))
        return EXIT_BAD_ARGS
    try:
        return asyncio.run(_amain(args))
    except Exception:
        traceback.print_exc()
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "A1_ABS_TOLERANCE_USD",
    "A1_REL_TOLERANCE",
    "DbCashSweep",
    "DbTrade",
    "ParsedArgs",
    "ReconcileReport",
    "StatementLine",
    "SweepMatch",
    "TradeMatch",
    "a1_tolerance",
    "build_report",
    "classify_line",
    "main",
    "match_trades",
    "parse_args",
    "parse_statement_rows",
    "reclassify_sweep_lines",
]
