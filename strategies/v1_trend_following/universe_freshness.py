"""Universe-freshness check for the V1 futures sub-universe.

Extracted from ``lean/v1_strategy.py::_log_universe_freshness`` (the inline
implementation shipped in PR #172) to gain unit-test coverage. ``lean/`` is
off the project's mypy/ruff/pytest target list per ``pyproject.toml``
because LEAN's ``AlgorithmImports`` injects symbols that mypy can't
resolve; this module is in ``strategies/`` which is on the python_backend
CI path filter and therefore gets full lint + type + test coverage.

**Context (2026-05-17 evening incident).** The 7 micro futures sub-
universe (`/MES /MNQ /MYM /M2K /MGC /MCL /MBT`) failed
``v1_history_unavailable`` for 5+ days running because the seed data had
aged out — universe files ended ``20260511.csv`` while the session_date
was ``2026-05-17``. The strategy's ``_build_bar_series`` returned
``None`` for each affected market and the markets were silently dropped
from ``active_universe``. The per-cycle heartbeat stayed clean and the
audit chain stayed clean throughout, so the staleness was invisible
until the operator inspected the LEAN cycle log directly. See
``Docs/decisions-log.md`` 2026-05-17 evening entry "7-micro
v1_history_unavailable root cause" for the full investigation.

**Why this extraction.** PR #172 shipped the defensive log inline in
``lean/v1_strategy.py`` because the CI ``python_backend`` path filter
was unable to gate cleanly past a pre-existing test failure
(``test_exit_full_close_end_to_end`` time-bomb on ``balance.snapshot_ts``).
PR #173 fixed the time-bomb. This PR repays the technical debt: pure
filesystem + date logic moves into a testable module; the wrapper in
``lean/v1_strategy.py`` keeps only the LEAN-runtime concerns
(``self.log`` + ``self.time.date()``).

``evaluate_universe_freshness(today, data_root, markets)`` runs once at
LEAN's ``initialize()`` (post-subscription setup) and produces a
classification of each market as fresh / stale / missing. The wrapper
in ``lean/v1_strategy.py`` emits structured ``self.log`` lines per
classification:

* ``v1_universe_data_fresh markets_checked=7 threshold_days=5
  fresh_count=7`` — happy path; all 7 markets within threshold.
* ``v1_universe_data_stale threshold_days=5 stale_count=<N>
  stale_markets=[<symbol>(<days>d/<file>),...]`` — one or more markets
  past threshold. **Canonical fingerprint** for the staleness pattern;
  an operator alert (Phase 1+ AsyncTaskMonitor extension per
  ``Docs/decisions-log.md`` 2026-05-17 evening entry follow-up #3) can
  grep for it.
* ``v1_universe_data_missing missing_markets=[...]`` — universe
  directory not found, no ``YYYYMMDD.csv`` entries, or unparseable
  filename. Distinct severity (volume-mount issue or seed-never-ran).

**Threshold calibration.** Default is 5 calendar days. The 2026-05-17
incident was 6 days stale and failed all 7 markets; setting the
threshold at > 5 days catches the next occurrence with 1-2 days of
headroom before LEAN's continuous-contract resolution starts dropping
markets. The exact LEAN behavior at the boundary is empirically
unknown (1-3 days may work; 6+ days definitely doesn't); the threshold
is conservative.

**A02 binding.** This module lives under ``strategies/v1_trend_following/``
which is NOT on the forbidden whitelist per dev-guide §11 (only
``services/risk/**`` etc.). The single forbidden-whitelist file inside
``strategies/v1_trend_following/`` is ``parameters.py`` (per Day-2
convention). This module is purely defensive observability — no
strategy or risk authority — and is hot-fix scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date as _date


@dataclass(frozen=True)
class MarketStaleness:
    """A single stale market with the staleness diagnostic context.

    Attributes:
        market: The market symbol (e.g., ``"/MES"``).
        last_file: The most recent universe filename found
            (e.g., ``"20260511.csv"``).
        days_stale: Calendar days between ``last_file``'s date and the
            session's ``today``. Always > 0; markets at or below the
            threshold are classified ``fresh`` instead.
    """

    market: str
    last_file: str
    days_stale: int


@dataclass(frozen=True)
class UniverseFreshnessResult:
    """Classification result for the V1 sub-universe at evaluation time.

    Disjoint partition of the input ``markets`` dict — each market
    appears in exactly one of ``fresh_markets`` / ``stale_markets`` /
    ``missing_markets``. ``len(fresh) + len(stale) + len(missing) ==
    len(input markets)``.

    The wrapper logs each non-empty category as a distinct structured
    log line so the operator (and any future log-analyzer) can
    distinguish "stale" (re-seed needed) from "missing" (volume mount
    or seed-never-ran).
    """

    fresh_markets: tuple[str, ...] = field(default_factory=tuple)
    stale_markets: tuple[MarketStaleness, ...] = field(default_factory=tuple)
    missing_markets: tuple[str, ...] = field(default_factory=tuple)


# Threshold calibrated against the 2026-05-17 incident (6-day staleness
# caused all 7 markets to fail). Warn at > 5 calendar days to give the
# operator 1-2 days of headroom before the cycle actually fails. Past
# experience: 0-day staleness (same-day seed) always works; 6+ day
# staleness always fails. The 1-5 day band is empirically unknown — the
# threshold is the conservative end.
DEFAULT_STALENESS_THRESHOLD_DAYS = 5


# Default market paths for the V1 futures sub-universe. Matches the
# layout produced by ``scripts/seed_lean_futures_databento.py`` per
# ``deploy/lean_local/seed-data.md`` Step F1. Keys are the canonical
# market symbols used in ``strategies/v1_trend_following/parameters.py``
# ``V1_CANDIDATE_UNIVERSE``; values are the relative path under the
# LEAN ``Data/future/`` root.
V1_FUTURES_MARKET_PATHS: dict[str, str] = {
    "/MES": "cme/universes/mes",
    "/MNQ": "cme/universes/mnq",
    "/MYM": "cme/universes/mym",
    "/M2K": "cme/universes/m2k",
    "/MBT": "cme/universes/mbt",
    "/MGC": "comex/universes/mgc",
    "/MCL": "nymex/universes/mcl",
}


def evaluate_universe_freshness(
    *,
    today: _date,
    data_root: str,
    markets: dict[str, str],
    threshold_days: int = DEFAULT_STALENESS_THRESHOLD_DAYS,
) -> UniverseFreshnessResult:
    """Scan futures universe directories + classify each market.

    Args:
        today: Session date to compute staleness against. Typically
            ``self.time.date()`` from inside LEAN's algorithm.
        data_root: Base directory containing the per-market subtree.
            Inside the LEAN container this is ``/Lean/Data/future``.
        markets: Mapping from market symbol (e.g., ``"/MES"``) to the
            relative subdir path under ``data_root`` (e.g.,
            ``"cme/universes/mes"``).
        threshold_days: Calendar-day staleness threshold. Markets where
            ``(today - last_file_date) > threshold_days`` are
            classified ``stale``; at or below threshold are ``fresh``.
            Defaults to ``DEFAULT_STALENESS_THRESHOLD_DAYS``.

    Returns:
        ``UniverseFreshnessResult`` with three disjoint tuples
        (``fresh_markets``, ``stale_markets``, ``missing_markets``).

    The function never raises. Missing ``data_root``, unreadable
    subdirs, malformed filenames, and any I/O error are all
    classified as ``missing`` and the function continues. This is by
    design — the freshness check is defensive observability and should
    never crash the algorithm's ``initialize()``.

    Filename convention: ``<YYYYMMDD>.csv`` (12 chars including the
    extension). The script ``scripts/seed_lean_futures_databento.py``
    emits these via ``write_universe_files()``; the format is locked
    by LEAN's continuous-contract resolution at
    ``DataMappingMode.OPEN_INTEREST``.
    """
    fresh: list[str] = []
    stale: list[MarketStaleness] = []
    missing: list[str] = []
    for market, subdir in markets.items():
        path = os.path.join(data_root, subdir)
        try:
            entries = os.listdir(path)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            missing.append(market)
            continue
        # Universe filenames are exactly ``YYYYMMDD.csv`` (8 digits + 4
        # for ``.csv`` = 12 chars total). Anything else is unexpected;
        # filter it out defensively rather than crash on the date parse.
        date_files = [e for e in entries if len(e) == 12 and e.endswith(".csv")]
        if not date_files:
            missing.append(market)
            continue
        # ``max()`` over ``YYYYMMDD.csv`` strings sorts lexicographically
        # which matches numeric date order because the format is
        # zero-padded fixed-width.
        last_file = max(date_files)
        try:
            last_date = _date(
                int(last_file[0:4]),
                int(last_file[4:6]),
                int(last_file[6:8]),
            )
        except (ValueError, IndexError):
            missing.append(market)
            continue
        days_stale = (today - last_date).days
        if days_stale > threshold_days:
            stale.append(
                MarketStaleness(
                    market=market,
                    last_file=last_file,
                    days_stale=days_stale,
                )
            )
        else:
            fresh.append(market)
    return UniverseFreshnessResult(
        fresh_markets=tuple(fresh),
        stale_markets=tuple(stale),
        missing_markets=tuple(missing),
    )


__all__ = [
    "DEFAULT_STALENESS_THRESHOLD_DAYS",
    "V1_FUTURES_MARKET_PATHS",
    "MarketStaleness",
    "UniverseFreshnessResult",
    "evaluate_universe_freshness",
]
