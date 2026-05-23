"""services/data/map_file_synthesis.py — synthesize LEAN futures map_files
from bar_sync's per-day universe-file history.

LEAN's continuous-contract resolver requires the futures map_file to
contain at least one row beyond the inception sentinel for the
``DataMappingMode`` the strategy queries (mode ``2`` = ``OpenInterest``
for the v1 strategy; see ``lean/v1_strategy.py::initialize``'s
``data_mapping_mode=DataMappingMode.OPEN_INTEREST`` on every
``add_future`` call). Without per-roll entries the resolver returns an
empty MapFile and ``self.history(continuous_symbol, count,
Resolution.DAILY)`` returns an empty DataFrame with empty columns.
This was the 2026-05-22 21:30 UTC cycle's failure mode for all 7
Phase 1 futures (probe captured ``hist_type=DataFrame hist_len=0
hist_cols=[]`` for ``/MES /MNQ /MYM /M2K /MGC /MCL /MBT``; ETFs
returned ``hist_len=205 hist_cols=['close','high','low','open',
'volume']`` from the same code path). See ``Docs/decisions-log.md``
2026-05-22 entry "Diagnostic probe (PR #220) CONFIRMS root cause" for
the full diagnostic chain.

This module is the operator-authorized fix outlined in that entry's
"Tomorrow's fix path (concrete)" subsection plus the 2026-05-22 follow-up
brief that motivated the LEAN-canonical ``<perm> <SID-hash>``
``MappedSymbol`` rendering. Pure-policy — no IBKR I/O, no DB — reads the
on-disk universe files :func:`services.data.bar_sync.write_futures_bundle`
already produces and writes a populated map_file alongside the existing
2-row sentinel.

Roll detection algorithm
------------------------

Each universe file at ``<data_root>/future/<market_dir>/universes/
<ticker>/<YYYYMMDD>.csv`` records the front-month ``expiry`` (YYYYMM)
bar_sync's ``reqContractDetails`` selected for that ET session day
(see :func:`services.data.bar_sync.pick_front_month_expiry`). Across
multi-year history these transitions are NOISY: bar_sync's selection
flip-flops between adjacent contracts on day-to-day volume/OI
fluctuations. For ``/MES`` over the operator's ~2y window, the raw
transition count is ~66 (oscillating between 202512 and 202606 ~14
times in a 6-week stretch); the actual quarterly roll count is ~6.

To filter noise, this module groups consecutive same-expiry sessions
into "runs" and DROPS any run shorter than ``persistence_days``
(default 15 trading sessions ≈ 3 weeks). The remaining "stable" runs
mark genuine front-month residences; transitions between them are
genuine roll boundaries. The brief recommends N=15 (operator-tested
to collapse /MES's noisy 66 → ~6).

Map_file output format (validated against LEAN's reference data)
----------------------------------------------------------------

LEAN's parser is in ``QuantConnect/Lean/Common/Data/Auxiliary/
MapFileRow.cs::Parse``::

    csv.Length >= 2: date + MappedSymbol
    csv.Length >= 3: + Exchange code
    csv.Length >= 4: + DataMappingMode integer

The format we render for each ticker::

    18991230,<perm_lower>,<EXCHANGE>                              ← inception sentinel (bare permtick)
    <YYYYMMDD>,<perm_lower> <sid_hash>,<EXCHANGE>,2               ← per-roll row, mode=OpenInterest
    ...
    20501231,<perm_lower> <last_sid_hash>,<EXCHANGE>,2            ← end sentinel carries last roll's SID

The ``MappedSymbol`` column on per-roll rows is the LEAN-canonical
``<perm> <SID-hash>`` form (e.g. ``mes uik2f7cj4v0h`` — space-separated,
permtick lowercased, SID hash lowercased). LEAN's
:class:`LiveSynchronizer` parses this column via
``SecurityIdentifier.Parse`` which mandates the two-part format;
emitting bare ``mes`` on per-roll rows crashes the parser (the 2026-05-22
PR #223 deploy proved this — runtime crash ``The string must be
splittable on space into two parts in SecurityIdentifier.cs:line 818``
forced the revert PR #224). The inception sentinel STAYS bare (matches
LEAN's bundled reference data — e.g. ``Data/future/cme/map_files/es.csv``
starts with ``18991230,es,CME``).

The SID hash is the base36-encoded representation of LEAN's bit-packed
``otherData`` field per ``QuantConnect/Lean/Common/SecurityIdentifier.cs``
``GenerateFuture``::

    otherData = (oadate(expiry_date) * DAYS_OFFSET)
              + (market_id * MARKET_OFFSET)
              + (SECURITY_TYPE_FUTURE * SECURITY_TYPE_OFFSET)

with constants from ``SecurityIdentifier.cs`` lines 79-101. Validated
against LEAN's bundled ``Data/future/cme/map_files/es.csv`` —
55/55 historical ES contracts reproduce byte-for-byte (see
``tests/unit/test_map_file_synthesis.py::TestComputeFutureSidHash``).

Per-product expiry rules (mirrored from ``QuantConnect/Lean/Common/
Securities/Future/FuturesExpiryFunctions.cs``):

* /MES, /MNQ, /MYM, /M2K — quarterly (Mar/Jun/Sep/Dec), 3rd Friday of
  contract month, ``AddBusinessDaysIfHoliday(-1)`` if the 3rd Friday
  is itself a holiday (e.g. /MES Jun 2026: 3rd Friday = 2026-06-19 =
  Juneteenth → expiry = 2026-06-18 Thursday)
* /MGC — bi-monthly (Feb/Apr/Jun/Aug/Oct/Dec), 3rd-last business day
  of contract month, ``AddBusinessDaysIfHoliday(-1)`` if itself a
  holiday
* /MCL — monthly, trading terminates 4 business days prior to the 25th
  calendar day of the month PRIOR to the contract month; if the 25th
  itself is a holiday, subtract 5 business days instead
* /MBT — monthly, last Friday of contract month,
  ``AddBusinessDaysIfHoliday(-1)`` if itself a holiday

Holiday set sourced from LEAN's ``Data/market-hours/market-hours-database.json``
(``Future-<market>-<TICKER>`` entries, union of ``holidays`` +
``bankHolidays`` fields), restricted to the 2020-2030 window —
sufficient for any roll boundary in bar_sync's universe-data window
(operator's bar_sync history spans ~2y back from today).

Idempotency
-----------

:func:`synthesize_futures_map_file` reads the existing map_file (if
any), compares to the freshly-rendered content, and skips the write
entirely when they match (preserves mtime — important so downstream
tooling that checks "did this file change?" doesn't get false
positives on no-op cycles). When content differs, write goes via a
temp file + atomic rename.

A-rule compliance
-----------------

* **A01** — no plaintext secrets; pure file-IO over the shared
  ``lean_data`` volume.
* **A02** — ``services/data/**`` is NOT on the forbidden whitelist;
  regular PR review applies.
* **A03** — ``structlog`` only; no ``print``, no stdlib ``logging``.
* **A05** — no ``float`` for prices; this module handles dates +
  expiry strings + integer base36 arithmetic only.
* **A06** — :class:`datetime.date` objects (not :class:`datetime.datetime`);
  no tz needed since session-date is a calendar date, not a wall clock.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Locked constants — LEAN format + roll-detection threshold + SID encoding
# ---------------------------------------------------------------------------


#: LEAN's :class:`DataMappingMode` integer for ``OpenInterest`` — verified
#: against ``QuantConnect/Lean/Common/Global.cs`` enum declaration order
#: (Base=0, Equity=1, Option=2, Commodity=3, Forex=4, Future=5; and inside
#: DataMappingMode: LastTradingDay=0, FirstDayMonth=1, OpenInterest=2,
#: OpenInterestAnnual=3). The v1 strategy queries
#: ``DataMappingMode.OPEN_INTEREST`` on every ``add_future`` call (see
#: ``lean/v1_strategy.py::initialize`` line 261 area), so this is the only
#: mode whose rows the resolver consults at history-call time.
DATA_MAPPING_MODE_OPEN_INTEREST: Final[int] = 2

#: Operator-recommended persistence filter from the 2026-05-22 brief
#: (``HANDOFF_PROMPT_bar_sync_mapfile_fix.md`` lines 44-46). Detected raw
#: transitions are dropped unless the NEW expiry persists for at least
#: this many consecutive trading sessions. For /MES this collapses ~66
#: raw transitions over 2y → ~6 genuine quarterly roll boundaries.
DEFAULT_PERSISTENCE_DAYS: Final[int] = 15

#: LEAN's inception-sentinel date (futures map_file convention; mirrors
#: bar_sync's existing 2-row sentinel + the post-pivot DataBento seeder's
#: output preserved in git history).
INCEPTION_SENTINEL_DATE: Final[date] = date(1899, 12, 30)

#: LEAN's end-sentinel date (far-future).
END_SENTINEL_DATE: Final[date] = date(2050, 12, 31)


# --- LEAN SecurityIdentifier bit-packing constants
# (from QuantConnect/Lean/Common/SecurityIdentifier.cs lines 79-101)

#: Offset used to pack SecurityType in the lowest decimal place.
_SECURITY_TYPE_OFFSET: Final[int] = 1

#: Offset used to pack the Market integer identifier (HardcodedMarkets in Market.cs).
_MARKET_OFFSET: Final[int] = 100

#: Offset used to pack the expiry date as the .NET OADate integer (days since 1899-12-30).
_DAYS_OFFSET: Final[int] = 100_000_000_000_000

#: SecurityType.Future enum value (from QuantConnect/Lean/Common/Global.cs).
_SECURITY_TYPE_FUTURE: Final[int] = 5

#: Market integer identifiers from ``QuantConnect/Lean/Common/Market.cs``
#: ``HardcodedMarkets``. Keyed by the lowercase market name (matching
#: the on-disk ``market_dir`` convention + LEAN's ``Market`` constants).
_MARKET_IDS: Final[dict[str, int]] = {
    "usa": 1,
    "nymex": 7,
    "cbot": 8,
    "comex": 22,
    "cme": 23,
}


# --- LEAN future contract month cycles
# (from QuantConnect/Lean/Common/Securities/Future/FutureExpirationCycles.cs)

#: Quarterly cycle (March/June/September/December) used by /MES, /MNQ, /MYM, /M2K.
_HMUZ_MONTHS: Final[frozenset[int]] = frozenset({3, 6, 9, 12})

#: Bi-monthly cycle (Feb/Apr/Jun/Aug/Oct/Dec) used by /MGC (micro gold).
_GJMQVZ_MONTHS: Final[frozenset[int]] = frozenset({2, 4, 6, 8, 10, 12})


# --- Holiday calendar
# Sourced from LEAN's Data/market-hours/market-hours-database.json (union of
# ``holidays`` + ``bankHolidays`` per ``Future-<market>-<TICKER>`` entry),
# restricted to 2020-2030. Sufficient for any roll boundary in bar_sync's
# universe-data window (operator's history spans ~2y back from today).

_BASE_FUTURES_HOLIDAYS_2020_2030: Final[frozenset[date]] = frozenset(
    {
        date(2020, 1, 1),
        date(2020, 1, 20),
        date(2020, 2, 17),
        date(2020, 5, 25),
        date(2020, 7, 3),
        date(2020, 9, 7),
        date(2020, 10, 12),
        date(2020, 11, 11),
        date(2020, 11, 26),
        date(2021, 1, 18),
        date(2021, 2, 15),
        date(2021, 5, 31),
        date(2021, 7, 5),
        date(2021, 9, 6),
        date(2021, 10, 11),
        date(2021, 11, 11),
        date(2021, 11, 25),
        date(2021, 12, 31),
        date(2022, 1, 17),
        date(2022, 2, 21),
        date(2022, 5, 30),
        date(2022, 6, 20),
        date(2022, 7, 4),
        date(2022, 9, 5),
        date(2022, 10, 10),
        date(2022, 11, 11),
        date(2022, 11, 24),
        date(2022, 12, 26),
        date(2023, 1, 16),
        date(2023, 2, 20),
        date(2023, 5, 29),
        date(2023, 6, 19),
        date(2023, 7, 4),
        date(2023, 9, 4),
        date(2023, 10, 9),
        date(2023, 11, 11),
        date(2023, 11, 23),
        date(2023, 12, 25),
        date(2024, 1, 1),
        date(2024, 1, 15),
        date(2024, 2, 19),
        date(2024, 5, 27),
        date(2024, 6, 19),
        date(2024, 7, 4),
        date(2024, 9, 2),
        date(2024, 10, 14),
        date(2024, 11, 11),
        date(2024, 11, 28),
        date(2024, 12, 25),
        date(2025, 1, 1),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 10, 13),
        date(2025, 11, 11),
        date(2025, 11, 27),
        date(2025, 12, 25),
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2027, 1, 1),
    }
)

#: Per-ticker holiday additions beyond the common base. Sourced from
#: per-ticker MHDB entries (``Future-cme-MES`` etc.); restricted to
#: 2020-2030. Mostly tiny — most tickers add Jan 2 2023 (observed New
#: Year's), some add Good Friday Apr 3 2026 (metals + crude), some
#: add Labor Day Sep 7 2026.
_PER_TICKER_HOLIDAY_EXTRA_2020_2030: Final[dict[str, frozenset[date]]] = {
    "MES": frozenset({date(2023, 1, 2), date(2026, 9, 7)}),
    "MNQ": frozenset({date(2023, 1, 2), date(2026, 9, 7)}),
    "MYM": frozenset({date(2023, 1, 2), date(2026, 9, 7)}),
    "M2K": frozenset({date(2023, 1, 2), date(2026, 9, 7)}),
    "MGC": frozenset({date(2023, 1, 2), date(2026, 4, 3)}),
    "MCL": frozenset({date(2023, 1, 2), date(2026, 4, 3), date(2026, 9, 7)}),
    "MBT": frozenset({date(2026, 9, 7)}),
}


def _holidays_for_ticker(ticker: str) -> frozenset[date]:
    """Return the LEAN-canonical holiday set for ``ticker`` (uppercase, no slash).

    Falls back to the common base set for tickers without explicit
    overrides — useful for tests and for any future expansion of the
    Phase 1 universe.
    """
    extra = _PER_TICKER_HOLIDAY_EXTRA_2020_2030.get(ticker.upper(), frozenset())
    return _BASE_FUTURES_HOLIDAYS_2020_2030 | extra


# ---------------------------------------------------------------------------
# Pure-policy data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UniverseSession:
    """A single (session_date, front-month expiry) pair from a universe file.

    Attributes:
        session_date: ET calendar date from the universe filename's
            ``<YYYYMMDD>.csv``.
        expiry: front-month expiry as ``YYYYMM`` (e.g., ``"202606"``)
            from the universe file's data-row first column.
    """

    session_date: date
    expiry: str


@dataclass(frozen=True, slots=True)
class FuturesRollTransition:
    """A genuine front-month roll boundary surviving the persistence filter.

    Attributes:
        boundary_date: the first ET session day on which the new expiry
            became the stable front-month (NOT the prior contract's
            last-trading day; we use the new contract's first-stable-day
            as the LEAN map_file row's date).
        from_expiry: prior stable expiry (``YYYYMM``).
        to_expiry: new stable expiry (``YYYYMM``).
        persisted_sessions: length of the new-expiry run (must be
            ``>= persistence_days``; included for observability).
    """

    boundary_date: date
    from_expiry: str
    to_expiry: str
    persisted_sessions: int


@dataclass(frozen=True, slots=True)
class MapFileSynthesisResult:
    """Outcome of one synthesis pass for one futures market."""

    ticker: str
    market_dir: str
    market_code: str
    rolls: tuple[FuturesRollTransition, ...]
    map_file_path: Path
    content_changed: bool


# ---------------------------------------------------------------------------
# Pure-policy: LEAN SID-hash helpers
# ---------------------------------------------------------------------------


def oadate(d: date) -> int:
    """Return .NET ``DateTime.ToOADate()`` integer days for ``d``.

    The .NET OADate epoch is 1899-12-30 (Lotus 1-2-3 compatibility);
    for any date on or after 1900-03-01 the simple subtraction matches
    .NET's value exactly. Pre-1900-03-01 dates trigger an explicit
    error rather than risk silent wrong output (no Phase 1 future has
    an expiry that old, so this guard never trips in production).

    Validated against multiple LEAN-bundled reference SIDs — e.g.
    ``oadate(2009-12-18) == 40165`` (ES Dec 2009 contract → SID
    ``uik2f7cj4v0h``).
    """
    if d < date(1900, 3, 1):
        raise ValueError(
            f"oadate not supported for dates before 1900-03-01 (got {d!r}); "
            ".NET ToOADate has a Lotus 1-2-3 leap-year quirk before this date."
        )
    return (d - date(1899, 12, 30)).days


def encode_base36(n: int) -> str:
    """Encode a non-negative integer as uppercase base36.

    Mirrors LEAN's ``QuantConnect/Lean/Common/Extensions.cs::EncodeBase36``
    (which pushes digits onto a stack then reverses on output).
    Returns the empty string for ``n == 0``. Raises ``ValueError`` on
    negative input.
    """
    if n < 0:
        raise ValueError(f"encode_base36 requires non-negative input, got {n!r}")
    if n == 0:
        return ""
    digits: list[str] = []
    while n:
        n, r = divmod(n, 36)
        digits.append(chr(ord("0") + r) if r < 10 else chr(ord("A") + r - 10))
    return "".join(reversed(digits))


def compute_future_sid_hash(*, expiry_date: date, market_dir: str) -> str:
    """Compute the lowercased base36 SID hash for a futures contract.

    Mirrors ``SecurityIdentifier.GenerateFuture(expiry, symbol, market)``
    in ``QuantConnect/Lean/Common/SecurityIdentifier.cs``::

        otherData = (oadate(expiry) * DAYS_OFFSET)
                  + (market_id * MARKET_OFFSET)
                  + (SECURITY_TYPE_FUTURE * SECURITY_TYPE_OFFSET)

    Strike / strike-scale / option-style / put-call all zero for
    futures → not added. Result is base36-encoded (uppercase per LEAN)
    then lowercased to match the on-disk ``MapFileRow.ToCsv()``
    convention (LEAN does ``MappedSymbol.ToLowerInvariant()`` when
    writing).

    Validated against LEAN's bundled ``Data/future/cme/map_files/es.csv``
    — 55/55 historical ES contracts reproduce byte-for-byte. See
    ``tests/unit/test_map_file_synthesis.py::TestComputeFutureSidHash``.

    Raises ``ValueError`` if ``market_dir`` is unknown (the caller
    almost certainly meant ``"cme"`` / ``"comex"`` / ``"nymex"`` /
    ``"cbot"`` and we'd rather fail loudly than emit a wrong SID).
    """
    market_lower = market_dir.lower()
    if market_lower not in _MARKET_IDS:
        raise ValueError(
            f"market_dir {market_dir!r} not in known LEAN markets; "
            f"expected one of {sorted(_MARKET_IDS)!r}"
        )
    market_id = _MARKET_IDS[market_lower]
    other_data = (
        (oadate(expiry_date) * _DAYS_OFFSET)
        + (market_id * _MARKET_OFFSET)
        + (_SECURITY_TYPE_FUTURE * _SECURITY_TYPE_OFFSET)
    )
    return encode_base36(other_data).lower()


# ---------------------------------------------------------------------------
# Pure-policy: expiry-date helpers (mirror LEAN's
# FuturesExpiryUtilityFunctions.cs)
# ---------------------------------------------------------------------------


def _is_common_business_day(d: date) -> bool:
    """Mirror LEAN's ``Extensions.cs::IsCommonBusinessDay`` (Sat/Sun = False)."""
    return d.weekday() < 5


def _add_business_days(d: date, n: int, holidays: frozenset[date]) -> date:
    """Mirror LEAN's ``FuturesExpiryUtilityFunctions.cs::AddBusinessDays``.

    Adds ``n`` business days (positive or negative) to ``d``, skipping
    weekends and holidays. The semantics intentionally include the
    ``d.AddDays(±totalDays)`` final step from LEAN's C# implementation —
    LEAN counts business days from day-after-d (positive) or
    day-before-d (negative), NOT including d itself in the count.
    """
    if n == 0:
        return d
    if n < 0:
        business_days = -n
        total_days = 1
        while True:
            candidate = d - timedelta(days=total_days)
            if candidate not in holidays and _is_common_business_day(candidate):
                business_days -= 1
                if business_days == 0:
                    return d - timedelta(days=total_days)
            total_days += 1
    else:
        business_days = n
        total_days = 1
        while True:
            candidate = d + timedelta(days=total_days)
            if candidate not in holidays and _is_common_business_day(candidate):
                business_days -= 1
                if business_days == 0:
                    return d + timedelta(days=total_days)
            total_days += 1


def _add_business_days_if_holiday(d: date, n: int, holidays: frozenset[date]) -> date:
    """Mirror ``AddBusinessDaysIfHoliday`` — adjust only if ``d`` is itself a holiday."""
    if d in holidays:
        return _add_business_days(d, n, holidays)
    return d


def _not_holiday(d: date, holidays: frozenset[date]) -> bool:
    """Mirror LEAN's ``NotHoliday`` — business day AND not in holiday list."""
    return _is_common_business_day(d) and d not in holidays


def _nth_friday_of_month(year: int, month: int, n: int) -> date:
    """Return the n-th Friday of (year, month). Mirror's LEAN's ``NthFriday``."""
    if n < 1 or n > 5:
        raise ValueError(f"n must be in [1, 5], got {n!r}")
    d = date(year, month, 1)
    days_to_first_fri = (4 - d.weekday()) % 7  # Friday = weekday 4
    candidate = d + timedelta(days=days_to_first_fri + 7 * (n - 1))
    if candidate.month != month:
        raise ValueError(f"{n}th Friday does not exist in {year}-{month:02d}")
    return candidate


def _third_friday_of_month(year: int, month: int) -> date:
    """Third Friday of (year, month) — quarterly index futures expiry primitive."""
    return _nth_friday_of_month(year, month, 3)


def _last_friday_of_month(year: int, month: int) -> date:
    """Last Friday of (year, month) — /MBT crypto futures expiry primitive."""
    # LEAN's LastWeekday walks day-of-month descending until it finds the target weekday.
    # Equivalent: find the LAST Friday by counting backward from the last day.
    daysinmonth = (
        (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - date(year, month, 1)
    ).days
    for day in range(daysinmonth, 0, -1):
        d = date(year, month, day)
        if d.weekday() == 4:
            return d
    raise AssertionError("unreachable: every month has a Friday")  # pragma: no cover


def _nth_last_business_day(year: int, month: int, n: int, holidays: frozenset[date]) -> date:
    """Mirror LEAN's ``NthLastBusinessDay`` — the n-th-last biz day of (year, month).

    ``n=1`` is the last business day, ``n=2`` is 2nd-last, ``n=3`` is
    3rd-last (the /MGC rule). Holidays count as non-business days.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n!r}")
    daysinmonth = (
        (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - date(year, month, 1)
    ).days
    if n > daysinmonth:
        raise ValueError(f"n ({n}) > days in month ({daysinmonth})")
    last_day_of_month = date(year, month, daysinmonth)
    # LEAN walks downward from last_day_of_month, decrementing n only when the
    # candidate is a business day AND not a holiday. The final return is
    # last_day_of_month.AddDays(-totalDays), where totalDays is the count of
    # calendar days walked.
    business_days = n
    total_days = 0
    while True:
        candidate = last_day_of_month - timedelta(days=total_days)
        if _not_holiday(candidate, holidays) and candidate not in holidays:
            business_days -= 1
            if business_days == 0:
                return last_day_of_month - timedelta(days=total_days)
        total_days += 1


# ---------------------------------------------------------------------------
# Pure-policy: per-product expiry rules
# (mirror QuantConnect/Lean/Common/Securities/Future/FuturesExpiryFunctions.cs)
# ---------------------------------------------------------------------------


def _expiry_micro_index_quarterly(*, year: int, month: int, holidays: frozenset[date]) -> date:
    """Expiry rule for /MES, /MNQ, /MYM, /M2K (quarterly index micro futures).

    The contract month is forced to the next HMUZ (Mar/Jun/Sep/Dec)
    month at or after ``(year, month)``, mirroring LEAN's:

        while (!HMUZ.Contains(time.Month)) time = time.AddMonths(1);

    Then takes the 3rd Friday of that month, with -1 biz day if the
    3rd Friday is itself a holiday (e.g. /MES Jun 2026: 3rd Friday =
    2026-06-19 = Juneteenth → expiry = 2026-06-18 Thursday).
    """
    # Roll to next HMUZ month
    while month not in _HMUZ_MONTHS:
        month += 1
        if month > 12:
            month -= 12
            year += 1
    third_friday = _third_friday_of_month(year, month)
    return _add_business_days_if_holiday(third_friday, -1, holidays)


def _expiry_micro_gold(*, year: int, month: int, holidays: frozenset[date]) -> date:
    """Expiry rule for /MGC (micro gold, COMEX, bi-monthly Feb/Apr/Jun/Aug/Oct/Dec).

    Per LEAN: roll to next GJMQVZ (Feb/Apr/Jun/Aug/Oct/Dec) month,
    then 3rd-last business day of contract month, with -1 biz day if
    itself a holiday.
    """
    while month not in _GJMQVZ_MONTHS:
        month += 1
        if month > 12:
            month -= 12
            year += 1
    nlbd = _nth_last_business_day(year, month, 3, holidays)
    return _add_business_days_if_holiday(nlbd, -1, holidays)


def _expiry_micro_crude_oil(*, year: int, month: int, holidays: frozenset[date]) -> date:
    """Expiry rule for /MCL (micro WTI crude oil, NYMEX, monthly).

    Per LEAN ``FuturesExpiryFunctions.cs`` ``MicroCrudeOilWTI`` block::

        previousMonth = contract_month.AddMonths(-1)
        twentyFifthDay = new DateTime(previousMonth.Year, previousMonth.Month, 25)
        businessDays = -4
        if NOT NotHoliday(twentyFifthDay, holidays): businessDays -= 1
        return AddBusinessDays(twentyFifthDay, businessDays, holidays)
    """
    prev_year = year if month > 1 else year - 1
    prev_month = month - 1 if month > 1 else 12
    twenty_fifth = date(prev_year, prev_month, 25)
    business_days = -4
    if not _not_holiday(twenty_fifth, holidays):
        business_days -= 1
    return _add_business_days(twenty_fifth, business_days, holidays)


def _expiry_micro_bitcoin(*, year: int, month: int, holidays: frozenset[date]) -> date:
    """Expiry rule for /MBT (micro Bitcoin, CME, monthly).

    Per LEAN ``MicroBTC`` block: last Friday of contract month, -1
    biz day if itself a holiday.
    """
    last_fri = _last_friday_of_month(year, month)
    return _add_business_days_if_holiday(last_fri, -1, holidays)


#: Per-ticker expiry rule lookup. Key is the bare uppercase ticker
#: (no leading slash, matching the form returned by
#: ``bar_sync.PHASE1_UNIVERSE_METADATA[...].ibkr_symbol``).
_EXPIRY_RULES: Final[dict[str, Callable[..., date]]] = {
    "MES": _expiry_micro_index_quarterly,
    "MNQ": _expiry_micro_index_quarterly,
    "MYM": _expiry_micro_index_quarterly,
    "M2K": _expiry_micro_index_quarterly,
    "MGC": _expiry_micro_gold,
    "MCL": _expiry_micro_crude_oil,
    "MBT": _expiry_micro_bitcoin,
}


def compute_future_expiry(*, ticker: str, contract_month: str) -> date:
    """Compute the expiry date for ``ticker`` + ``contract_month`` (YYYYMM).

    Looks up the per-product rule from :data:`_EXPIRY_RULES` and the
    per-ticker holiday set from :data:`_PER_TICKER_HOLIDAY_EXTRA_2020_2030`.
    Raises ``ValueError`` for unknown tickers + malformed contract_month
    strings.
    """
    if len(contract_month) != 6 or not contract_month.isdigit():
        raise ValueError(f"contract_month must be a 6-digit YYYYMM string, got {contract_month!r}")
    year = int(contract_month[:4])
    month = int(contract_month[4:6])
    if year < 1900 or year > 2100 or month < 1 or month > 12:
        raise ValueError(
            f"contract_month {contract_month!r} parses to invalid (year, month) = ({year}, {month})"
        )
    rule = _EXPIRY_RULES.get(ticker.upper())
    if rule is None:
        raise ValueError(
            f"no expiry rule registered for ticker {ticker!r}; "
            f"known tickers: {sorted(_EXPIRY_RULES)!r}"
        )
    holidays = _holidays_for_ticker(ticker)
    return rule(year=year, month=month, holidays=holidays)


# ---------------------------------------------------------------------------
# Pure-policy: universe-file parsing
# ---------------------------------------------------------------------------


def _parse_session_date_from_filename(filename: str) -> date | None:
    """Extract the ET session date from a universe filename.

    bar_sync writes universe files as ``<YYYYMMDD>.csv`` (see
    :func:`services.data.bar_sync.futures_universe_file_path`). Files
    whose stem is not 8 digits are silently skipped (defensive against
    operator-dropped files in the universe dir).
    """
    if not filename.endswith(".csv"):
        return None
    stem = filename[:-4]
    if len(stem) != 8 or not stem.isdigit():
        return None
    try:
        return date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))
    except ValueError:
        return None


def _parse_expiry_from_universe_body(body: str) -> str | None:
    """Read the front-month expiry (``YYYYMM``) from a universe-file body.

    Expected shape (1 header line starting with ``#`` + 1 data line):

        #expiry,open,high,low,close,volume,open_interest
        202606,7467.5,7524,7466.5,7484,1148753,292401

    The first non-``#`` non-blank line's first comma-separated field is
    treated as the expiry. Returns None on malformed input — caller
    skips that session.

    The 6-digit YYYYMM is validated (4-digit year between 1900 and 2100;
    2-digit month between 01 and 12) to defend against garbled rows.
    """
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        first_field = line.split(",", 1)[0].strip()
        if len(first_field) != 6 or not first_field.isdigit():
            return None
        year = int(first_field[:4])
        month = int(first_field[4:6])
        if year < 1900 or year > 2100 or month < 1 or month > 12:
            return None
        return first_field
    return None


def parse_universe_file(path: Path) -> str | None:
    """Read a universe file from disk and return its expiry, or None.

    Pure-IO helper; tests typically use :func:`_parse_expiry_from_universe_body`
    directly with synthetic strings.
    """
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_expiry_from_universe_body(body)


def iter_universe_sessions(universe_dir: Path) -> Iterator[UniverseSession]:
    """Walk ``universe_dir`` in ascending session-date order, yielding parsed sessions.

    Universe files whose filename doesn't match ``<YYYYMMDD>.csv``, or
    whose body fails to parse, are silently skipped. The output is
    sorted by ``session_date`` ascending. Re-reads are cheap; the
    caller is free to consume the iterator once.
    """
    if not universe_dir.is_dir():
        return
    parsed: list[UniverseSession] = []
    for entry in os.scandir(universe_dir):
        if not entry.is_file():
            continue
        session_date = _parse_session_date_from_filename(entry.name)
        if session_date is None:
            continue
        expiry = parse_universe_file(Path(entry.path))
        if expiry is None:
            continue
        parsed.append(UniverseSession(session_date=session_date, expiry=expiry))
    parsed.sort(key=lambda s: s.session_date)
    yield from parsed


# ---------------------------------------------------------------------------
# Pure-policy: roll detection (the heart of the noise filter)
# ---------------------------------------------------------------------------


def detect_real_rolls(
    sessions: Iterable[UniverseSession],
    *,
    persistence_days: int = DEFAULT_PERSISTENCE_DAYS,
) -> list[FuturesRollTransition]:
    """Detect genuine front-month roll boundaries with a persistence filter.

    Algorithm:

    1. Group sessions (assumed sorted by ``session_date`` ascending — the
       caller is responsible) into RUNS of consecutive same-expiry days.
    2. Drop runs shorter than ``persistence_days`` — these are noise
       (day-to-day flip-flopping in bar_sync's ``reqContractDetails``
       front-month picker).
    3. Walk the surviving stable runs in order; each adjacent pair is
       a genuine roll boundary, dated at the new run's first session.

    Returns an empty list when no genuine rolls are detected (e.g., a
    single-expiry history, or every transition is noise). The caller is
    responsible for combining the result with the inception/end
    sentinels.

    Raises ``ValueError`` if ``persistence_days <= 0`` (would treat every
    transition as genuine, defeating the filter).
    """
    if persistence_days <= 0:
        raise ValueError(f"persistence_days must be >= 1, got {persistence_days!r}")

    sessions_list = list(sessions)
    if not sessions_list:
        return []

    # Step 1: group into runs.
    runs: list[tuple[int, str, int]] = []  # (start_idx, expiry, length)
    i = 0
    while i < len(sessions_list):
        start = i
        expiry = sessions_list[i].expiry
        while i < len(sessions_list) and sessions_list[i].expiry == expiry:
            i += 1
        runs.append((start, expiry, i - start))

    # Step 2: keep only runs persisting at least persistence_days sessions.
    stable_runs = [r for r in runs if r[2] >= persistence_days]

    # Step 3: build transitions between adjacent stable runs. Skip pairs
    # whose expiry is unchanged — they're stable runs that bracket a
    # filtered-noise run (e.g., E1[30] → E2[5 noise] → E1[30]) and don't
    # represent a real roll.
    rolls: list[FuturesRollTransition] = []
    for k in range(1, len(stable_runs)):
        _prev_start, prev_expiry, _prev_len = stable_runs[k - 1]
        curr_start, curr_expiry, curr_len = stable_runs[k]
        if prev_expiry == curr_expiry:
            continue
        rolls.append(
            FuturesRollTransition(
                boundary_date=sessions_list[curr_start].session_date,
                from_expiry=prev_expiry,
                to_expiry=curr_expiry,
                persisted_sessions=curr_len,
            )
        )
    return rolls


# ---------------------------------------------------------------------------
# Pure-policy: map_file content rendering
# ---------------------------------------------------------------------------


def _format_map_file_date(d: date) -> str:
    return f"{d:%Y%m%d}"


def _resolve_market_dir(*, market_code: str, market_dir: str | None) -> str:
    """Resolve the on-disk market directory for SID computation.

    The synthesizer historically threaded ``market_code`` (uppercase
    ``"CME"``/``"COMEX"``/``"NYMEX"``) — this lowercases to the
    matching ``market_dir``. Pass ``market_dir`` explicitly to
    override (e.g., /MYM lives in ``cme`` on disk but its LEAN
    canonical market may be ``cbot`` per ``FuturesExpiryFunctions.cs``
    registration; the operator's brief recommends ``cme`` until the
    deployed runtime tells us otherwise).
    """
    return (market_dir if market_dir is not None else market_code).lower()


def build_futures_map_file_with_rolls(
    *,
    ticker: str,
    market_code: str,
    rolls: Iterable[FuturesRollTransition],
    data_mapping_mode_int: int = DATA_MAPPING_MODE_OPEN_INTEREST,
    market_dir: str | None = None,
    emit_sid_hash: bool = True,
) -> str:
    """Render a LEAN-format futures map_file as a string.

    Structure (validated against ``QuantConnect/Lean/Data/future/cme/
    map_files/es.csv`` and the ``MapFileRow.Parse`` source)::

        18991230,<lower>,<MARKET>                                ← inception sentinel (bare permtick)
        <YYYYMMDD>,<lower> <sid_hash>,<MARKET>,<mode>            ← per roll, SID-hash MappedSymbol
        ...
        20501231,<lower> <last_sid_hash>,<MARKET>,<mode>         ← end sentinel carries last SID

    The inception sentinel intentionally omits the ``DataMappingMode``
    column AND the SID hash so LEAN's ``GetMappedSymbol`` returns
    ``MappedSymbol=<lower>`` for any mode query before the first real
    roll boundary. Per-roll rows carry the LEAN-canonical
    ``<perm> <SID-hash>`` MappedSymbol (space-separated, both lowercased)
    so ``SecurityIdentifier.Parse`` accepts them — the 2026-05-22 PR
    #223 deploy proved that emitting bare permticks on per-roll rows
    crashes the parser with "The string must be splittable on space
    into two parts in SecurityIdentifier.cs:line 818".

    The end sentinel carries the LAST roll's SID forward to
    ``20501231`` (matches LEAN's bundled ``es.csv`` convention — its
    end sentinel uses the most recent contract's SID). When ``rolls``
    is empty, the end sentinel degrades to a bare permtick (no SID to
    propagate); LEAN's resolver then has nothing to map and the strategy
    can't construct a continuous future, but the file at least parses
    cleanly — important because bar_sync writes a sentinel-only file
    the first time it sees a new ticker before any roll boundaries are
    detected.

    Set ``emit_sid_hash=False`` to fall back to the pre-2026-05-22 PR
    #222 bare-permtick form on per-roll rows. Reserved for testing
    + the unlikely contingency where the SID-hash form breaks LEAN in
    a way the operator hasn't anticipated; in production this should
    always stay ``True``.

    Args:
        ticker: bare symbol (e.g., ``"MES"``) — case-insensitive; the
            output is always lowercased. Drives the per-product expiry
            rule lookup.
        market_code: LEAN market code (e.g., ``"CME"``, ``"COMEX"``,
            ``"NYMEX"``); uppercase preserved as-is in the output's
            3rd CSV column.
        rolls: detected transitions in ascending boundary-date order.
        data_mapping_mode_int: LEAN ``DataMappingMode`` integer; defaults
            to ``2`` (``OpenInterest``).
        market_dir: override for the SID-hash market lookup; defaults
            to ``market_code.lower()``. Pass explicitly when the
            on-disk directory + canonical LEAN market disagree (e.g.,
            /MYM lives in ``cme`` on disk per bar_sync's metadata).
        emit_sid_hash: when True (default), emit per-roll rows in the
            LEAN-canonical ``<perm> <SID-hash>`` form. When False,
            emit bare permticks (PR #222's pre-fix form).

    Returns the map_file content as a string with trailing newline.

    Raises ``ValueError`` from :func:`compute_future_sid_hash` /
    :func:`compute_future_expiry` if any roll's ``to_expiry`` doesn't
    parse + the ticker/market_dir are unknown (only when
    ``emit_sid_hash=True``).
    """
    lower = ticker.lower()
    resolved_market_dir = _resolve_market_dir(market_code=market_code, market_dir=market_dir)

    lines: list[str] = []
    lines.append(f"{_format_map_file_date(INCEPTION_SENTINEL_DATE)},{lower},{market_code}")

    rolls_list = list(rolls)
    last_sid_hash: str | None = None
    for roll in rolls_list:
        if emit_sid_hash:
            expiry_date = compute_future_expiry(ticker=ticker, contract_month=roll.to_expiry)
            sid_hash = compute_future_sid_hash(
                expiry_date=expiry_date, market_dir=resolved_market_dir
            )
            mapped_symbol = f"{lower} {sid_hash}"
            last_sid_hash = sid_hash
        else:
            mapped_symbol = lower
        lines.append(
            f"{_format_map_file_date(roll.boundary_date)},{mapped_symbol},"
            f"{market_code},{data_mapping_mode_int}"
        )

    # End sentinel: bare permtick when no rolls; SID-tagged otherwise.
    if last_sid_hash is None:
        end_mapped = lower
    else:
        end_mapped = f"{lower} {last_sid_hash}"
    lines.append(
        f"{_format_map_file_date(END_SENTINEL_DATE)},{end_mapped},"
        f"{market_code},{data_mapping_mode_int}"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# I/O orchestrator (idempotent + atomic)
# ---------------------------------------------------------------------------


def _read_existing_map_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return None


def _write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via tmp + rename.

    Creates the parent directory if missing. The tmp file is created
    in the same directory as the target so ``os.rename`` stays on the
    same filesystem (atomic on POSIX).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile auto-deletes on close (delete=False overrides).
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, path)


def synthesize_futures_map_file(
    *,
    data_root: Path,
    ticker: str,
    market_dir: str,
    market_code: str,
    persistence_days: int = DEFAULT_PERSISTENCE_DAYS,
    data_mapping_mode_int: int = DATA_MAPPING_MODE_OPEN_INTEREST,
    emit_sid_hash: bool = True,
) -> MapFileSynthesisResult:
    """Synthesize one futures map_file from the on-disk universe history.

    Reads ``<data_root>/future/<market_dir>/universes/<ticker_lower>/``,
    detects rolls via :func:`detect_real_rolls`, renders the LEAN-format
    content with SID-hash MappedSymbol entries via
    :func:`build_futures_map_file_with_rolls`, and writes to
    ``<data_root>/future/<market_dir>/map_files/<ticker_lower>.csv``
    atomically. If the existing file content is byte-identical to the
    freshly-rendered content, the write is skipped (idempotent —
    preserves mtime).

    Returns a :class:`MapFileSynthesisResult` with the detected rolls
    + the actual on-disk path + a ``content_changed`` flag. Never
    raises on missing universe directory (returns 0 rolls + writes
    a sentinel-only map_file if no on-disk data exists yet).

    Why we still write a sentinel-only map_file on empty universe:
    the existing ``services.data.bar_sync.build_futures_map_file``
    produces a 2-row sentinel without ``DataMappingMode``; this
    synthesizer's sentinel-only output (inception + end with mode=2)
    is structurally richer. We prefer this module's output even when
    no rolls are detected so the on-disk format is consistent across
    "data still ingesting" and "data fully populated" states.

    The SID-hash MappedSymbol path is on by default (``emit_sid_hash=True``).
    The fallback bare-permtick form is preserved for testing + the
    unlikely contingency where SID emission breaks LEAN; in production
    this should always stay ``True``.

    Logs a single structured ``futures_map_file_synthesized`` event
    with ticker / market_code / roll_count / content_changed for
    observability — the operator can grep this line per cycle.
    """
    bound = log.bind(
        op="synthesize_futures_map_file",
        ticker=ticker.lower(),
        market_dir=market_dir,
        market_code=market_code,
        emit_sid_hash=emit_sid_hash,
    )
    universe_dir = data_root / "future" / market_dir / "universes" / ticker.lower()
    sessions = list(iter_universe_sessions(universe_dir))
    rolls = detect_real_rolls(sessions, persistence_days=persistence_days)
    content = build_futures_map_file_with_rolls(
        ticker=ticker,
        market_code=market_code,
        rolls=rolls,
        data_mapping_mode_int=data_mapping_mode_int,
        market_dir=market_dir,
        emit_sid_hash=emit_sid_hash,
    )
    map_file_path = data_root / "future" / market_dir / "map_files" / f"{ticker.lower()}.csv"
    existing = _read_existing_map_file(map_file_path)
    content_changed = existing != content
    if content_changed:
        _write_atomic(map_file_path, content)
    bound.info(
        "futures_map_file_synthesized",
        roll_count=len(rolls),
        sessions_scanned=len(sessions),
        content_changed=content_changed,
        map_file_path=str(map_file_path),
        persistence_days=persistence_days,
    )
    return MapFileSynthesisResult(
        ticker=ticker.lower(),
        market_dir=market_dir,
        market_code=market_code,
        rolls=tuple(rolls),
        map_file_path=map_file_path,
        content_changed=content_changed,
    )


__all__ = [
    "DATA_MAPPING_MODE_OPEN_INTEREST",
    "DEFAULT_PERSISTENCE_DAYS",
    "END_SENTINEL_DATE",
    "INCEPTION_SENTINEL_DATE",
    "FuturesRollTransition",
    "MapFileSynthesisResult",
    "UniverseSession",
    "build_futures_map_file_with_rolls",
    "compute_future_expiry",
    "compute_future_sid_hash",
    "detect_real_rolls",
    "encode_base36",
    "iter_universe_sessions",
    "oadate",
    "parse_universe_file",
    "synthesize_futures_map_file",
]
