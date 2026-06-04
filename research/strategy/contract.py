"""The resolution-agnostic strategy contract (design D7).

A strategy maps a :class:`~research.data.bars.BarSeries` to a per-bar target
position in INTEGER CONTRACTS. The contract is intentionally tiny so the same
implementation runs unchanged at any resolution and so a strategy can be both
evaluated by the simple daily evaluator (research-only, P1) and — from P2 —
expressed to LEAN (the authority).

**Look-ahead rule (design §5.5).** ``target_positions(series)[t]`` may use bars
``[0..t]`` only (decided at/after ``close[t]``). The evaluator is responsible for
the entry shift (it applies a one-bar lag so a position decided at bar ``t``
earns P&L only from ``t`` onward); a strategy must NOT use ``close[t]`` to act
within bar ``t``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np
import numpy.typing as npt

from research.data.bars import BarSeries


class ResearchStrategy(ABC):
    """Base class for a resolution-agnostic research strategy."""

    #: Human-readable identifier, surfaced in reports.
    name: str = "unnamed"

    @property
    def warmup_bars(self) -> int:
        """Bars of history required before the first actionable signal (default 0)."""
        return 0

    @abstractmethod
    def target_positions(self, series: BarSeries) -> npt.NDArray[np.int64]:
        """Return target integer-contract position per bar (length ``len(series)``).

        Element ``t`` is the position DESIRED as of ``close[t]`` using only bars
        ``[0..t]``. Positive = long, negative = short, 0 = flat.
        """
        raise NotImplementedError
