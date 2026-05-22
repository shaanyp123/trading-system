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
"Tomorrow's fix path (concrete)" subsection. Pure-policy — no IBKR
I/O, no DB — reads the on-disk universe files
:func:`services.data.bar_sync.write_futures_bundle` already produces
and writes a populated map_file alongside the existing 2-row sentinel.

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

    18991230,<perm_lower>,<EXCHANGE>                 ← inception sentinel (no mode)
    <YYYYMMDD>,<perm_lower>,<EXCHANGE>,2             ← roll boundary, mode=OpenInterest
    ...
    20501231,<perm_lower>,<EXCHANGE>,2               ← end sentinel, mode=OpenInterest

The ``MappedSymbol`` column is just the bare lowercased permtick (e.g.
``mes``) — NOT the LEAN-reference SID-hash form (e.g. ``es
uik2f7cj4v0h``). LEAN's reference data uses the SID hash because each
historical contract has a distinct
:class:`QuantConnect.SecurityIdentifier`; replicating that requires
the Python port of LEAN's base36-encoded bit-packed
``date+market+securityType`` plus the per-exchange expiry-date rules
(third-Friday-of-month for index futures, varying for /MCL/MGC/MBT
energies/metals/crypto). Since LEAN's
``FutureUniverse.Reader`` reads the actual contract month from the
per-day universe file (``stream.GetDateTime(DateFormat.YearMonth)``)
and computes the Symbol via ``Symbol.CreateFuture(symbol, market,
expiry)`` independently of the map_file's MappedSymbol, the data-file
path resolution works without the SID hash. The map_file's
``MappedSymbol`` only drives the
:class:`SymbolChangedEvent`-style observable side (``Config.MappedSymbol``
updates on roll boundaries) — the v1 strategy doesn't subscribe to
those events.

If tomorrow's 21:30 UTC cycle's probe shows futures ``hist_len`` is
still 0 after this PR lands, the SID-hash form is the next fallback;
the probe in ``lean/v1_strategy.py::_log_history_probe`` is still
armed.

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
  expiry strings only.
* **A06** — :class:`datetime.date` objects (not :class:`datetime.datetime`);
  no tz needed since session-date is a calendar date, not a wall clock.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Locked constants — LEAN format + roll-detection threshold
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


def build_futures_map_file_with_rolls(
    *,
    ticker: str,
    market_code: str,
    rolls: Iterable[FuturesRollTransition],
    data_mapping_mode_int: int = DATA_MAPPING_MODE_OPEN_INTEREST,
) -> str:
    """Render a LEAN-format futures map_file as a string.

    Structure (validated against ``QuantConnect/Lean/Data/future/cme/
    map_files/es.csv`` and the ``MapFileRow.Parse`` source)::

        18991230,<lower>,<MARKET>
        <YYYYMMDD>,<lower>,<MARKET>,<mode>      ← per roll
        ...
        20501231,<lower>,<MARKET>,<mode>

    The inception sentinel has NO ``DataMappingMode`` column so LEAN's
    ``GetMappedSymbol`` returns ``MappedSymbol=<lower>`` for any mode
    query before the first real roll boundary. The per-roll rows carry
    the integer mode (``2`` for ``OpenInterest`` by default). The end
    sentinel uses the same mode so any query past the last roll still
    resolves cleanly.

    The ``MappedSymbol`` column is the bare lowercased ticker (no
    SID-hash suffix) — see the module docstring for the rationale.

    Args:
        ticker: bare symbol (e.g., ``"MES"``) — case-insensitive; the
            output is always lowercased.
        market_code: LEAN market code (e.g., ``"CME"``, ``"COMEX"``,
            ``"NYMEX"``); uppercase preserved as-is.
        rolls: detected transitions in ascending boundary-date order.
        data_mapping_mode_int: LEAN ``DataMappingMode`` integer; defaults
            to ``2`` (``OpenInterest``).

    Returns the map_file content as a string with trailing newline.
    """
    lower = ticker.lower()
    lines: list[str] = []
    lines.append(f"{_format_map_file_date(INCEPTION_SENTINEL_DATE)},{lower},{market_code}")
    for roll in rolls:
        lines.append(
            f"{_format_map_file_date(roll.boundary_date)},{lower},"
            f"{market_code},{data_mapping_mode_int}"
        )
    lines.append(
        f"{_format_map_file_date(END_SENTINEL_DATE)},{lower},{market_code},{data_mapping_mode_int}"
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
) -> MapFileSynthesisResult:
    """Synthesize one futures map_file from the on-disk universe history.

    Reads ``<data_root>/future/<market_dir>/universes/<ticker_lower>/``,
    detects rolls via :func:`detect_real_rolls`, renders the LEAN-format
    content, and writes to ``<data_root>/future/<market_dir>/map_files/
    <ticker_lower>.csv`` atomically. If the existing file content is
    byte-identical to the freshly-rendered content, the write is
    skipped (idempotent — preserves mtime).

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

    Logs a single structured ``futures_map_file_synthesized`` event
    with ticker / market_code / roll_count / content_changed for
    observability — the operator can grep this line per cycle.
    """
    bound = log.bind(
        op="synthesize_futures_map_file",
        ticker=ticker.lower(),
        market_dir=market_dir,
        market_code=market_code,
    )
    universe_dir = data_root / "future" / market_dir / "universes" / ticker.lower()
    sessions = list(iter_universe_sessions(universe_dir))
    rolls = detect_real_rolls(sessions, persistence_days=persistence_days)
    content = build_futures_map_file_with_rolls(
        ticker=ticker,
        market_code=market_code,
        rolls=rolls,
        data_mapping_mode_int=data_mapping_mode_int,
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
    "detect_real_rolls",
    "iter_universe_sessions",
    "parse_universe_file",
    "synthesize_futures_map_file",
]
