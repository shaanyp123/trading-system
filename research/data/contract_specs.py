"""Per-symbol contract reference data (design §5.3).

The single source of truth for notional / leverage / $-per-tick math. Money-
adjacent spec values (multiplier, tick size) are ``Decimal`` — they are NOT
covered by the design-D8 float exemption, which is scoped to derived analytics.
The active set mirrors ``strategies/v1_trend_following/parameters.py::
V1_CANDIDATE_UNIVERSE`` exactly (6 micro futures + 4 bond ETFs; ``/MCL`` and any
sidelined market excluded); a unit test pins this so the two never drift.

``market_dir`` is the LEAN on-disk directory under ``future/<market_dir>/`` and
matters: ``/MYM`` lives under ``cbot``, not ``cme`` (getting it wrong yields a
silent empty history — the 2026-05-25 PR #226 bug). ETFs use the equity path
(``equity/usa/``); their ``market_dir`` is ``"usa"`` for symmetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

AssetClass = Literal["future", "etf"]


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Reference data for one tradeable symbol."""

    symbol: str  # "/MES" (futures, leading slash) or "TLT" (ETF, bare)
    name: str
    asset_class: AssetClass
    multiplier: Decimal  # point value: notional = contracts * multiplier * price
    tick_size: Decimal  # minimum price increment
    currency: str
    market_dir: str  # LEAN on-disk dir: cme / cbot / comex / usa

    @property
    def dollars_per_tick(self) -> Decimal:
        """USD value of one tick for one contract (``multiplier * tick_size``)."""
        return self.multiplier * self.tick_size

    def notional(self, contracts: int, price: Decimal) -> Decimal:
        """Gross notional of ``contracts`` at ``price`` (always ``Decimal``)."""
        return Decimal(contracts) * self.multiplier * price


_SPECS: Final[tuple[ContractSpec, ...]] = (
    # --- CME / CBOT / COMEX micro futures ---------------------------------
    ContractSpec(
        "/MES", "E-mini S&P 500 Micro", "future", Decimal("5"), Decimal("0.25"), "USD", "cme"
    ),
    ContractSpec(
        "/MNQ", "E-mini Nasdaq-100 Micro", "future", Decimal("2"), Decimal("0.25"), "USD", "cme"
    ),
    ContractSpec(
        "/MYM", "E-mini Dow Micro", "future", Decimal("0.5"), Decimal("1.0"), "USD", "cbot"
    ),
    ContractSpec(
        "/M2K", "E-mini Russell 2000 Micro", "future", Decimal("5"), Decimal("0.1"), "USD", "cme"
    ),
    ContractSpec("/MGC", "Gold Micro", "future", Decimal("10"), Decimal("0.1"), "USD", "comex"),
    ContractSpec("/MBT", "Bitcoin Micro", "future", Decimal("0.1"), Decimal("5.0"), "USD", "cme"),
    # --- NYSE bond ETFs (cash equity; no contract roll) -------------------
    ContractSpec(
        "TLT", "iShares 20+ Year Treasury", "etf", Decimal("1"), Decimal("0.01"), "USD", "usa"
    ),
    ContractSpec(
        "IEF", "iShares 7-10 Year Treasury", "etf", Decimal("1"), Decimal("0.01"), "USD", "usa"
    ),
    ContractSpec(
        "SHY", "iShares 1-3 Year Treasury", "etf", Decimal("1"), Decimal("0.01"), "USD", "usa"
    ),
    ContractSpec("TIP", "iShares TIPS Bond", "etf", Decimal("1"), Decimal("0.01"), "USD", "usa"),
)

SPECS: Final[dict[str, ContractSpec]] = {s.symbol: s for s in _SPECS}


def get_spec(symbol: str) -> ContractSpec:
    """Return the :class:`ContractSpec` for ``symbol`` or raise ``KeyError``.

    Symbols are exact: futures carry the leading slash (``"/MES"``); ETFs are
    bare (``"TLT"``). This mirrors ``V1_CANDIDATE_UNIVERSE`` key style.
    """
    try:
        return SPECS[symbol]
    except KeyError:
        raise KeyError(f"no ContractSpec for {symbol!r}; known symbols: {sorted(SPECS)}") from None


def is_future(symbol: str) -> bool:
    return get_spec(symbol).asset_class == "future"
