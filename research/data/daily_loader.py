"""Read daily bars from a COPY of the api's on-disk LEAN-format data.

The on-disk format is re-implemented here from the documented spec (it is NOT
imported from ``services/data/bar_sync.py``, which pulls ``ib_async``). Layout
mirrors what ``bar_sync`` writes:

* **ETF** — ``equity/usa/daily/<lower>.zip`` containing one member ``<lower>.csv``
  with lines ``YYYYMMDD 00:00,O*10000,H*10000,L*10000,C*10000,V`` (prices are
  deci-cent integer-scaled — ``$85.56`` ⇒ ``855600``; divide by 10000 on read).
* **Futures (single expiry)** — ``future/<market_dir>/daily/<lower>_trade.zip``
  containing one member PER EXPIRY named ``<lower>_trade_<YYYYMM>.csv`` with
  lines ``YYYYMMDD 00:00,O,H,L,C,V`` (RAW float prices).

P1 loads a single clean daily series: full history for an ETF, or ONE expiry for
a future. CONTINUOUS-contract stitching across rolls is deliberately NOT done
here — that is LEAN's job (design D1) and lands with the LEAN driver + parity
rail in P2, so a stitched series can be validated against LEAN immediately
instead of trusting a second roll engine.
"""

from __future__ import annotations

import zipfile
from datetime import date, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt
import structlog

from research.data.bars import BarSeries
from research.data.contract_specs import SPECS

_log = structlog.get_logger(__name__)

#: Equity-daily prices are integer-scaled by this factor (LEAN convention).
_EQUITY_PRICE_SCALE = 10000.0


def _filename_stem(symbol: str) -> str:
    """``"/MES"`` → ``"mes"``; ``"TLT"`` → ``"tlt"``."""
    return symbol.lstrip("/").lower()


def _resolve_market_dir(symbol: str, override: str | None) -> str:
    if override is not None:
        return override
    spec = SPECS.get(symbol)
    if spec is not None:
        return spec.market_dir
    return "usa" if not symbol.startswith("/") else "cme"


def _equity_zip_path(data_root: Path, symbol: str) -> Path:
    return data_root / "equity" / "usa" / "daily" / f"{_filename_stem(symbol)}.zip"


def _futures_zip_path(data_root: Path, symbol: str, market_dir: str) -> Path:
    return data_root / "future" / market_dir / "daily" / f"{_filename_stem(symbol)}_trade.zip"


def _select_futures_member(names: list[str], stem: str, expiry: str | None) -> str:
    """Pick the ``<stem>_trade_<YYYYMM>.csv`` member; require disambiguation.

    Only members matching this ``stem``'s ``<stem>_trade_`` prefix count — a
    mis-packed zip (a foreign instrument's CSV inside ``<stem>_trade.zip``) must
    NOT load silently as the wrong instrument (the silent-wrong-history class
    design §5.3 warns about).
    """
    prefix = f"{stem}_trade_"
    members = sorted(n for n in names if n.startswith(prefix) and n.endswith(".csv"))
    if expiry is not None:
        want = f"{prefix}{expiry}.csv"
        if want not in members:
            raise KeyError(f"expiry {expiry!r} not in {stem}_trade.zip; available: {members}")
        return want
    if len(members) == 1:
        return members[0]
    raise ValueError(
        f"{stem}_trade.zip has {len(members)} expiries {members} matching {prefix!r}; "
        "pass expiry=... to pick one (continuous stitching across rolls is LEAN's job, P2)"
    )


def _parse_csv(
    body: str, *, price_scale: float
) -> tuple[list[date], list[float], list[float], list[float], list[float], list[float]]:
    dts: list[date] = []
    o: list[float] = []
    h: list[float] = []
    low: list[float] = []
    c: list[float] = []
    v: list[float] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split(",")
        if len(fields) != 6:
            raise ValueError(f"malformed bar line (expected 6 fields): {line!r}")
        day_token = fields[0].split(" ", 1)[0]
        dts.append(datetime.strptime(day_token, "%Y%m%d").date())
        o.append(float(fields[1]) / price_scale)
        h.append(float(fields[2]) / price_scale)
        low.append(float(fields[3]) / price_scale)
        c.append(float(fields[4]) / price_scale)
        v.append(float(fields[5]))
    if not dts:
        raise ValueError("no bars parsed (empty series)")
    return dts, o, h, low, c, v


def load_daily_series(
    data_root: Path,
    symbol: str,
    *,
    expiry: str | None = None,
    start: date | None = None,
    end: date | None = None,
    market_dir: str | None = None,
) -> BarSeries:
    """Load a single daily :class:`BarSeries` for ``symbol`` from ``data_root``.

    ``data_root`` is the root of a COPY of the LEAN on-disk tree (never the live
    volume). For futures, pass ``expiry="YYYYMM"`` unless the zip holds exactly
    one expiry. ``start``/``end`` filter inclusively. Raises ``FileNotFoundError``
    if the zip is absent, ``KeyError`` for a missing expiry, ``ValueError`` for a
    malformed/empty/ambiguous series.
    """
    is_future = symbol.startswith("/")
    if is_future:
        mkt = _resolve_market_dir(symbol, market_dir)
        zip_path = _futures_zip_path(data_root, symbol, mkt)
        price_scale = 1.0
    else:
        zip_path = _equity_zip_path(data_root, symbol)
        price_scale = _EQUITY_PRICE_SCALE

    if not zip_path.exists():
        raise FileNotFoundError(f"no on-disk daily zip for {symbol} at {zip_path}")

    stem = _filename_stem(symbol)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if is_future:
            member = _select_futures_member(names, stem, expiry)
        else:
            member = f"{stem}.csv"
            if member not in names:
                raise KeyError(f"member {member!r} not in {zip_path}; have {sorted(names)}")
        body = zf.read(member).decode("utf-8")

    dts, o, h, low, c, v = _parse_csv(body, price_scale=price_scale)

    # Inclusive date filter.
    keep = [
        i for i, d in enumerate(dts) if (start is None or d >= start) and (end is None or d <= end)
    ]
    if not keep:
        raise ValueError(f"{symbol}: no bars in [{start}, {end}]")

    def _arr(values: list[float]) -> npt.NDArray[np.float64]:
        return np.asarray([values[i] for i in keep], dtype=np.float64)

    series = BarSeries(
        symbol=symbol,
        dates=tuple(dts[i] for i in keep),
        open=_arr(o),
        high=_arr(h),
        low=_arr(low),
        close=_arr(c),
        volume=_arr(v),
        resolution="daily",
        price_treatment="raw",
        expiry=expiry if is_future else None,
    )
    _log.debug(
        "research_daily_series_loaded",
        symbol=symbol,
        bars=len(series),
        start=str(series.start),
        end=str(series.end),
        expiry=series.expiry,
    )
    return series
