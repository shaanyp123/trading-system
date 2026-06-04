"""``BarSeries`` — the in-memory daily bar container the harness passes around.

numpy-array-backed (no pandas — it is absent from the research venv and a hard
dependency would trip nothing useful at daily volume). OHLCV are
``float64`` arrays under the design D8 analytics exemption; dates are an
ascending tuple of ``datetime.date``. One ``BarSeries`` is one instrument at one
resolution (daily for P1).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class BarSeries:
    """An ascending daily OHLCV series for a single instrument.

    Invariant: ``len(dates) == open.shape[0] == high.shape[0] == low.shape[0]
    == close.shape[0] == volume.shape[0]`` and ``dates`` is strictly ascending.
    Construct via :func:`research.data.daily_loader.load_daily_series` (which
    enforces the invariant) rather than directly in production paths.

    ``price_treatment`` records how prices are presented so a report never
    silently mixes conventions (design §5.1): ``"raw"`` = un-adjusted per-expiry
    futures prices (the V1 default) or raw ETF closes; future back-/ratio-
    adjusted treatments would be a distinct, reported value.
    """

    symbol: str
    dates: tuple[date, ...]
    open: npt.NDArray[np.float64]
    high: npt.NDArray[np.float64]
    low: npt.NDArray[np.float64]
    close: npt.NDArray[np.float64]
    volume: npt.NDArray[np.float64]
    resolution: str = "daily"
    price_treatment: str = "raw"
    #: For futures single-expiry P1 loads, the ``YYYYMM`` expiry of the contract
    #: the series came from; ``None`` for ETFs / continuous series.
    expiry: str | None = None

    def __len__(self) -> int:
        return len(self.dates)

    def __post_init__(self) -> None:
        n = len(self.dates)
        if n == 0:
            raise ValueError(f"BarSeries[{self.symbol}] requires at least one bar")
        for name in ("open", "high", "low", "close", "volume"):
            arr = getattr(self, name)
            if arr.shape != (n,):
                raise ValueError(f"BarSeries[{self.symbol}].{name} shape {arr.shape} != ({n},)")
        if n >= 2 and any(b <= a for a, b in itertools.pairwise(self.dates)):
            raise ValueError(f"BarSeries[{self.symbol}] dates not strictly ascending")

    @property
    def start(self) -> date:
        return self.dates[0]

    @property
    def end(self) -> date:
        return self.dates[-1]
