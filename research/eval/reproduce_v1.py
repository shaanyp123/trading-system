"""Reproduce production V1 + cross-check against the live paper signals oracle.

The trust bridge's second proof (design §8): drive LEAN to run the production
``V1TrendFollowingAlgorithm`` on a fixed daily window with the live params, then
check that the harness-captured backtest AGREES, at the DECISION level (same market
+ direction + session_date), with what live paper V1 actually decided — the prod
``signals`` table (``env='paper'``, ``signal_type='entry'``).

Per the P2 kickoff: reproduce at the DECISION level, not the fill — ``decision_price
≠ backtest fill`` is expected (that gap is what the §6.6 tolerances cover). NOTE
(measured, charter PR D): prod stamps a DISTINCT ``parameter_set_hash`` on EVERY
signal, so "restrict to a single phash" is not a usable filter. A clean cross-check
instead picks a parameter-REGIME window by DATE via :func:`crosscheck_entries`'s
``window`` filter — the live regime boundary is the Kaufman ER gate landing
2026-06-02 (before it, live ran WITHOUT the gate the current code applies, so the
backtest legitimately vetoes some pre-boundary live entries:
``efficiency_below_threshold``).

This module is LEAN-free + pure: it turns parsed :class:`OrderFill`s into entry
DECISIONS and diffs them against an oracle snapshot. The actual LEAN run is driven
by :func:`build_v1_run_spec` + ``research.lean.driver.run_backtest`` (isolated;
skip-gated).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog

from research.lean.config_render import load_production_v1_parameters
from research.lean.driver import LeanRunSpec
from research.lean.results import OrderFill

# Read-only import of the production candidate universe (importing strategies/ is
# allowed; modifying it is not). Seeds the log parser's universe — see below.
from strategies.v1_trend_following.parameters import V1_CANDIDATE_UNIVERSE

_log = structlog.get_logger(__name__)

#: Repo paths (read-only inputs) for the production V1 algorithm + its package.
_V1_ALGORITHM = Path("lean/v1_strategy.py")
_STRATEGIES_PKG = Path("strategies")
#: The repo ``services/`` tree — mounted read-only at ``/Lean/services`` so the V1
#: backtest-order path can load ``services/risk/sizing.py`` (the real Stage 0-5
#: sizer) by file path. See ``research/lean/driver.py::_CONTAINER_SERVICES``.
_SERVICES_PKG = Path("services")

# V1 emits decisions via HTTP POST and places NO LEAN orders, so its decisions live
# in the LEAN LOG, not the result JSON's orders/trades. Per cycle the log records one
# ``v1_signal_rejected ... market=X`` per rejected market + a ``v1_signals_generated
# ... signals_emitted_count=N`` summary; the N emitted markets are exactly
# (universe) - (rejected this cycle). These regexes parse that. (Direction is NOT
# captured from the log — neither line carries a side, so every derived entry gets
# the ``direction`` default label. V1 emits shorts too; capturing the true side
# needs POST-body capture, a future enhancement — ``crosscheck_entries`` warns when
# the oracle carries non-long entries.)
_REJECTED_RE = re.compile(r"v1_signal_rejected session_date=(\d{4}-\d{2}-\d{2}) market=(\S+)")
_GENERATED_RE = re.compile(
    r"v1_signals_generated session_date=(\d{4}-\d{2}-\d{2}) signals_emitted_count=(\d+)"
)


@dataclass(frozen=True, slots=True)
class Entry:
    """An entry DECISION: a market taken from flat to a directional position."""

    session_date: date
    market: str
    direction: str  # "long" | "short"

    def key(self) -> tuple[date, str, str]:
        return (self.session_date, self.market, self.direction)


def _normalize_direction(value: object) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("long", "buy"):
            return "long"
        if v in ("short", "sell"):
            return "short"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "long" if value > 0 else "short"
    return str(value).strip().lower()


def _find_v1_log(output_dir: Path) -> Path:
    """Locate the algorithm log that carries V1's per-cycle decisions."""
    for path in sorted(output_dir.glob("*log*.txt")):
        try:
            if "v1_signals_generated" in path.read_text(encoding="utf-8", errors="replace"):
                return path
        except OSError:
            continue
    raise FileNotFoundError(
        f"no V1 algorithm log (with 'v1_signals_generated') under {output_dir} — did the "
        "backtest run V1TrendFollowingAlgorithm?"
    )


def parse_v1_decisions_from_log(output_dir: Path, *, direction: str = "long") -> list[Entry]:
    """Extract V1 entry DECISIONS from the LEAN log (V1 places no orders; it POSTs).

    Per cycle: emitted markets = (universe) - (markets rejected that cycle), for
    cycles whose ``signals_emitted_count`` is non-zero. The universe is SEEDED from
    the production ``V1_CANDIDATE_UNIVERSE`` (a rejection-derived universe alone
    misses a market that is emitted EVERY cycle — never rejected, never seen — and
    its absence poisons the count check for whole cycles), unioned with the names
    the log actually rejects. When the elimination is still ambiguous (a market
    neither emitted nor rejected that cycle, e.g. a data error → count mismatch)
    the cycle is SKIPPED rather than fabricating an entry.
    """
    log = _find_v1_log(output_dir).read_text(encoding="utf-8", errors="replace").splitlines()
    rejected: dict[str, set[str]] = {}
    emitted_count: dict[str, int] = {}
    universe: set[str] = set(V1_CANDIDATE_UNIVERSE)
    for line in log:
        rej = _REJECTED_RE.search(line)
        if rej is not None:
            rejected.setdefault(rej.group(1), set()).add(rej.group(2))
            universe.add(rej.group(2))
            continue
        gen = _GENERATED_RE.search(line)
        if gen is not None:
            emitted_count[gen.group(1)] = int(gen.group(2))
    entries: list[Entry] = []
    for session, count in emitted_count.items():
        if count <= 0:
            continue
        emitted = universe - rejected.get(session, set())
        if len(emitted) != count:
            _log.warning(
                "research_v1_log_emit_ambiguous",
                session_date=session,
                emitted_count=count,
                derived=len(emitted),
            )
            continue
        for market in sorted(emitted):
            entries.append(Entry(date.fromisoformat(session), market, direction))
    _log.info("research_v1_decisions_from_log", decisions=len(entries))
    return entries


def first_entry_per_market(entries: list[Entry]) -> list[Entry]:
    """Reduce to the FIRST entry per (market, direction), ascending by date.

    Neutralizes the residual backtest-vs-live re-emit gap. Post-#335 the backtest
    DOES have position feedback (the order path snapshots LEAN holdings), so the
    blanket "re-emits every cycle" era is over; re-emits now arise only from
    roll-edge / pending-fill windows where the snapshot reads flat for a cycle or
    two (the live ``/positions`` GET stays live-mode only; anti-pyramiding live is
    PR #250). Comparing first-entries asks "did each market flag, and when first"
    rather than re-emit cadence — still the right level for the cross-check.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Entry] = []
    for entry in sorted(entries, key=lambda e: (e.session_date, e.market)):
        key = (entry.market, entry.direction)
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


def entries_from_fills(fills: list[OrderFill]) -> list[Entry]:
    """Reduce filled orders to entry decisions (flat → directional per market).

    NOTE: V1 places NO LEAN orders (it POSTs decisions), so for the V1 reproduction
    use :func:`parse_v1_decisions_from_log` instead — this fill-based path yields
    nothing for V1 and exists for reference strategies that DO place LEAN orders.

    The first fill that opens a market is the entry. A subsequent fill that flips
    direction (flat-crossing) also counts as a new entry; fills that merely close to
    flat do not (no pyramiding — anti-pyramiding guard, PR #250).
    """
    running: dict[str, int] = {}
    entries: list[Entry] = []
    for fill in sorted(fills, key=lambda f: (f.fill_date, f.market)):
        prev = running.get(fill.market, 0)
        cur = prev + fill.quantity
        if prev == 0 and cur != 0:
            entries.append(Entry(fill.fill_date, fill.market, "long" if cur > 0 else "short"))
        elif prev != 0 and cur != 0 and (prev > 0) != (cur > 0):
            entries.append(Entry(fill.fill_date, fill.market, "long" if cur > 0 else "short"))
        running[fill.market] = cur
    return entries


@dataclass(frozen=True, slots=True)
class OracleEntry:
    """One live paper entry decision from the prod ``signals`` table."""

    session_date: date
    market: str
    direction: str

    def key(self) -> tuple[date, str, str]:
        return (self.session_date, self.market, self.direction)


def load_oracle(path: Path) -> list[OracleEntry]:
    """Load an oracle snapshot (the reproduce-V1 SQL output, JSON-shaped).

    Accepts either ``{"entries": [...]}`` or a bare list. Each item needs
    ``session_date`` (``YYYY-MM-DD``), ``market``, ``direction``; a ``signal_type``
    other than ``entry``, if present, is filtered out.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("entries", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError(f"{path}: oracle must be a list or have an 'entries' list")
    out: list[OracleEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("signal_type", "entry")).lower() != "entry":
            continue
        out.append(
            OracleEntry(
                session_date=date.fromisoformat(str(item["session_date"])),
                market=str(item["market"]),
                direction=_normalize_direction(item.get("direction")),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class CrossCheckReport:
    """Decision-level agreement between the V1 backtest and the live oracle."""

    matched: tuple[tuple[date, str, str], ...]
    missing_in_backtest: tuple[tuple[date, str, str], ...]  # oracle decided, backtest didn't
    extra_in_backtest: tuple[tuple[date, str, str], ...]  # backtest decided, oracle didn't
    window_start: date | None
    window_end: date | None

    @property
    def oracle_count(self) -> int:
        return len(self.matched) + len(self.missing_in_backtest)

    @property
    def match_rate(self) -> float:
        return len(self.matched) / self.oracle_count if self.oracle_count else 1.0

    def summary(self) -> str:
        window = (
            f"[{self.window_start}..{self.window_end}]"
            if self.window_start or self.window_end
            else "[full]"
        )
        return (
            f"V1 repro {window}: matched {len(self.matched)}/{self.oracle_count} "
            f"({self.match_rate:.0%}); missing {len(self.missing_in_backtest)}, "
            f"extra {len(self.extra_in_backtest)}"
        )


def crosscheck_entries(
    backtest_entries: list[Entry],
    oracle_entries: list[OracleEntry],
    *,
    window: tuple[date, date] | None = None,
) -> CrossCheckReport:
    """Diff backtest vs oracle entry decisions (market + direction + session_date).

    ``window`` (inclusive) restricts BOTH sides to a single parameter-REGIME
    sub-window (cut by DATE — e.g. the ER gate landed live 2026-06-02) so a
    mid-window regime change does not muddy the comparison. Prod phashes are
    per-signal (PR D, measured), so the regime is identified by date, not hash.
    """
    # Log-derived backtest entries are all labeled "long" (direction is not in the
    # LEAN log); a short in the oracle can therefore falsely match/mismatch on
    # direction. Warn so a non-long oracle is read with that caveat.
    non_long = sorted({e.direction for e in oracle_entries if e.direction != "long"})
    if non_long:
        _log.warning(
            "research_v1_oracle_non_long_directions",
            directions=non_long,
            note="log-derived backtest entries carry a fixed 'long' label; "
            "direction-level matches against these oracle rows may be spurious",
        )
    start, end = window if window is not None else (None, None)

    def _in_window(d: date) -> bool:
        return (start is None or d >= start) and (end is None or d <= end)

    bt = {e.key() for e in backtest_entries if _in_window(e.session_date)}
    orc = {e.key() for e in oracle_entries if _in_window(e.session_date)}
    report = CrossCheckReport(
        matched=tuple(sorted(bt & orc)),
        missing_in_backtest=tuple(sorted(orc - bt)),
        extra_in_backtest=tuple(sorted(bt - orc)),
        window_start=start,
        window_end=end,
    )
    _log.info(
        "research_v1_repro_crosscheck",
        matched=len(report.matched),
        missing=len(report.missing_in_backtest),
        extra=len(report.extra_in_backtest),
        match_rate=round(report.match_rate, 4),
    )
    return report


def build_v1_run_spec(
    data_root: Path,
    *,
    parameters: dict[str, str] | None = None,
    start: date | None = None,
    end: date | None = None,
    starting_cash: float | None = None,
    v1_algorithm: Path = _V1_ALGORITHM,
    strategies_pkg: Path = _STRATEGIES_PKG,
    services_pkg: Path = _SERVICES_PKG,
) -> LeanRunSpec:
    """A :class:`LeanRunSpec` that reproduces production V1 (isolated; POST-stubbed).

    Drives the REPO's ``lean/v1_strategy.py`` (a read-only input, not copied) with
    the live params from ``lean/lean.json`` and the ``strategies/`` package mounted.
    ``posts_to_api=True`` forces the docker backend + the isolation env so V1's
    per-cycle POSTs never reach the prod api (design §9 P2).

    ``start`` / ``end`` set V1's backtest window via the ``BACKTEST_START_DATE`` /
    ``BACKTEST_END_DATE`` parameters (V1 reads them in ``initialize()``, defaulting to
    its original hard-coded paper window when absent). This is what lets the harness
    backtest the REAL V1 over any multi-year span. ``starting_cash`` flows the same
    way (``STARTING_CASH_USD``); ``None`` keeps lean.json's value.
    """
    params = dict(parameters or load_production_v1_parameters())
    if start is not None:
        params["BACKTEST_START_DATE"] = start.strftime("%Y%m%d")
    if end is not None:
        params["BACKTEST_END_DATE"] = end.strftime("%Y%m%d")
    if starting_cash is not None:
        params["STARTING_CASH_USD"] = str(int(starting_cash))
    return LeanRunSpec(
        algorithm_type_name="V1TrendFollowingAlgorithm",
        algorithm_source=v1_algorithm,
        parameters=params,
        data_root=data_root,
        symbol="PORTFOLIO",  # V1 is multi-instrument; equity curve is account-level
        multiplier=1.0,
        strategy_name="v1_trend_following",
        extra_package_mounts=(strategies_pkg,),
        service_package_mount=services_pkg,
        posts_to_api=True,
    )
