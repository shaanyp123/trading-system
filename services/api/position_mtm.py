"""services/api/position_mtm.py — on-read mark-to-market for open positions.

Backend-spec §4.1.5b ``Position.current_price`` + ``Position.unrealized_pnl``.

Phase 1 simplification: the api computes ``current_price`` per-request by
reading the latest bar close from the LEAN on-disk daily zips (the same
files :mod:`services.data.bar_sync` writes at 17:00 ET each session).
This avoids spinning up a separate MTM background worker for Phase 1
while still giving the home-page positions table real numbers.

Pricing semantics:

* **ETFs** — ``equity/usa/daily/<lower>.zip`` member ``<lower>.csv``;
  prices are integer-scaled by 10000 (deci-cents) per LEAN's equity-
  daily format. We divide by 10000 before returning.
* **Futures** — ``future/<market_dir>/daily/<lower>_trade.zip`` with
  member CSVs per contract month ``<lower>_trade_<YYYYMM>.csv``;
  prices are raw decimal. We pick the member whose last bar has the
  largest ``session_date`` (the current front-month CSV).

Fallback to ``avg_cost`` (and ``unrealized_pnl=0``) when:

* The market is not in :data:`services.api.exposure.V1_EXPOSURE_METADATA`
  (unknown — same fallback as the exposure path).
* The zip file is missing on disk (e.g., dev environment, sync skip,
  ETF only-just-added).
* The zip is empty / unreadable / has no recognizable CSV members.

Direction-aware PnL math:

* LONG  (qty > 0): ``unrealized_pnl = (current_price - avg_cost) * qty   * multiplier``
* SHORT (qty < 0): ``unrealized_pnl = (avg_cost - current_price) * |qty| * multiplier``
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

import structlog

from services.api.exposure import V1_EXPOSURE_METADATA
from services.data.bar_sync import (
    PHASE1_UNIVERSE_METADATA,
    equity_daily_zip_path,
    futures_trade_zip_path,
)

log = structlog.get_logger()


#: Deci-cent scale factor for LEAN equity-daily CSVs. ``2935600`` →
#: ``$293.56``. See :func:`services.data.bar_sync.build_equity_daily_csv`.
_EQUITY_DECI_CENTS_PER_DOLLAR: Final[Decimal] = Decimal("10000")

#: Match the futures contract-month suffix in a member name like
#: ``m2k_trade_202606.csv``. Group 1 captures the ``YYYYMM`` part for
#: tie-break sorting when multiple members have the same last-bar date.
_FUTURES_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9]+_trade_(\d{6})\.csv$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class PositionMtmResult:
    """Return shape for :func:`compute_position_mtm`.

    ``current_price`` is the latest bar's close in dollars; on missing
    data it falls back to ``avg_cost`` so the route caller can render
    the position row without a null. ``unrealized_pnl`` is signed (+
    for in-the-money longs / shorts; - for losing). ``source`` is the
    diagnostic string surfaced in structlog for missing-data debugging.
    """

    current_price: Decimal
    unrealized_pnl: Decimal
    source: str  # "fresh_bar" / "fallback_avg_cost" / "unknown_market"


def _read_last_bar_close(zip_path: Path, member_name: str) -> Decimal | None:
    """Open ``zip_path``, read ``member_name``, return last row's close.

    Returns ``None`` if the zip is missing, unreadable, the member
    doesn't exist, the CSV is empty, or the close field can't be
    parsed as a Decimal. All failure modes log at WARN with the
    specific reason.
    """
    if not zip_path.is_file():
        return None
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if member_name not in zf.namelist():
                log.warning(
                    "position_mtm_member_missing",
                    zip_path=str(zip_path),
                    member_name=member_name,
                )
                return None
            body = zf.read(member_name).decode("utf-8")
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("position_mtm_zip_read_failed", zip_path=str(zip_path), error=str(exc))
        return None

    last_line = body.rstrip("\n").rsplit("\n", 1)[-1]
    parts = last_line.split(",")
    if len(parts) < 5:
        log.warning("position_mtm_csv_row_malformed", zip_path=str(zip_path), last_line=last_line)
        return None
    try:
        return Decimal(parts[4])
    except (ValueError, ArithmeticError) as exc:
        log.warning(
            "position_mtm_close_unparseable",
            zip_path=str(zip_path),
            close_field=parts[4],
            error=str(exc),
        )
        return None


def _read_futures_latest_close(zip_path: Path) -> Decimal | None:
    """For a futures-daily zip, find the member whose last bar has the
    largest session_date + return that close.

    Multiple per-contract-month CSV members may coexist (PR #229
    historical-contract backfill). The current front-month's CSV has
    the most recent bars, so we sort by ``(last_session_date, YYYYMM)``
    descending and pick the top.
    """
    if not zip_path.is_file():
        return None
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = [name for name in zf.namelist() if _FUTURES_MEMBER_RE.match(name) is not None]
            if not members:
                log.warning("position_mtm_no_futures_members", zip_path=str(zip_path))
                return None
            best: tuple[str, str, Decimal] | None = None  # (last_date, yyyymm, close)
            for name in members:
                match = _FUTURES_MEMBER_RE.match(name)
                if match is None:
                    continue
                yyyymm = match.group(1)
                try:
                    body = zf.read(name).decode("utf-8")
                except (zipfile.BadZipFile, OSError) as exc:
                    log.warning(
                        "position_mtm_member_read_failed",
                        zip_path=str(zip_path),
                        member_name=name,
                        error=str(exc),
                    )
                    continue
                last_line = body.rstrip("\n").rsplit("\n", 1)[-1]
                parts = last_line.split(",")
                if len(parts) < 5:
                    continue
                # The first field looks like "20260527 00:00"; we only
                # need the YYYYMMDD prefix for the max-by comparison.
                last_date = parts[0].split(" ", 1)[0]
                try:
                    close = Decimal(parts[4])
                except (ValueError, ArithmeticError):
                    continue
                if best is None or (last_date, yyyymm) > (best[0], best[1]):
                    best = (last_date, yyyymm, close)
            return best[2] if best is not None else None
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("position_mtm_zip_read_failed", zip_path=str(zip_path), error=str(exc))
        return None


def fetch_current_price(market: str, data_root: Path) -> Decimal | None:
    """Return the latest on-disk bar close for ``market`` in dollars,
    or ``None`` if data is unavailable.

    Routes ETFs to ``equity_daily_zip_path`` + futures to
    ``futures_trade_zip_path`` (the same helpers
    :mod:`services.data.bar_sync` uses on the writer side, so reader
    + writer can't drift).
    """
    universe_meta = PHASE1_UNIVERSE_METADATA.get(market)
    if universe_meta is None:
        return None
    if universe_meta.kind == "etf":
        zip_path = equity_daily_zip_path(data_root, universe_meta.ibkr_symbol)
        member = f"{universe_meta.ibkr_symbol.lower()}.csv"
        raw_close = _read_last_bar_close(zip_path, member)
        if raw_close is None:
            return None
        return (raw_close / _EQUITY_DECI_CENTS_PER_DOLLAR).quantize(Decimal("0.0001"))
    # Futures branch
    zip_path = futures_trade_zip_path(
        data_root, universe_meta.ibkr_symbol, universe_meta.market_dir
    )
    raw_close = _read_futures_latest_close(zip_path)
    if raw_close is None:
        return None
    return raw_close.quantize(Decimal("0.0001"))


def compute_position_mtm(
    market: str,
    quantity: int,
    avg_cost: Decimal,
    data_root: Path,
) -> PositionMtmResult:
    """Compute (current_price, unrealized_pnl) for a single position.

    Returns ``PositionMtmResult`` with ``source`` indicating which
    path was taken (``fresh_bar`` / ``fallback_avg_cost`` /
    ``unknown_market``) for operator debugging.
    """
    exposure_meta = V1_EXPOSURE_METADATA.get(market)
    if exposure_meta is None:
        # Unknown market: same fallback as exposure path. multiplier=1
        # is a best-effort guess; matters for nothing here since we
        # return unrealized_pnl=0 anyway.
        log.warning("position_mtm_unknown_market", market=market)
        return PositionMtmResult(
            current_price=avg_cost,
            unrealized_pnl=Decimal("0"),
            source="unknown_market",
        )

    current_price = fetch_current_price(market, data_root)
    if current_price is None:
        return PositionMtmResult(
            current_price=avg_cost,
            unrealized_pnl=Decimal("0"),
            source="fallback_avg_cost",
        )

    # Direction-aware: a SHORT with qty=-1 gains when current_price drops
    # below avg_cost. The multiplication signs work out as long as we
    # use signed qty throughout: (current - avg) * qty * multiplier is
    # positive for an in-the-money LONG and positive for an in-the-money
    # SHORT (because both deltas are negative when qty is negative).
    qty_dec = Decimal(quantity)
    delta = current_price - avg_cost
    unrealized = (delta * qty_dec * exposure_meta.multiplier).quantize(Decimal("0.01"))

    return PositionMtmResult(
        current_price=current_price,
        unrealized_pnl=unrealized,
        source="fresh_bar",
    )


__all__ = [
    "PositionMtmResult",
    "compute_position_mtm",
    "fetch_current_price",
]
