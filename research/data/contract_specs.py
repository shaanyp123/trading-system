"""Per-symbol contract reference data (design §5.3).

The single source of truth for notional / leverage / $-per-tick math AND the
explicit research cost model (charter PR B). Money-adjacent spec values
(multiplier, tick size, commissions) are ``Decimal`` — they are NOT covered by
the design-D8 float exemption, which is scoped to derived analytics. The active
set mirrors ``strategies/v1_trend_following/parameters.py::
V1_CANDIDATE_UNIVERSE`` exactly (6 micro futures + 4 bond ETFs; ``/MCL`` and any
sidelined market excluded); a unit test pins this so the two never drift.

``market_dir`` is the LEAN on-disk directory under ``future/<market_dir>/`` and
matters: ``/MYM`` lives under ``cbot``, not ``cme`` (getting it wrong yields a
silent empty history — the 2026-05-25 PR #226 bug). ETFs use the equity path
(``equity/usa/``); their ``market_dir`` is ``"usa"`` for symmetry.

Cost model (charter PR B, empirically probed 2026-06-10 against the
``trading-lean-local`` image):

- ``commission_per_side`` (futures) is the ALL-IN per-contract per-side cost on
  IBKR fixed pricing, non-member: IBKR commission + exchange fee + $0.02
  NFA/regulatory. As of 2026-06: index micros $0.25 + $0.35 + $0.02 = $0.62;
  MGC $0.25 + $1.10 + $0.02 = $1.37; MBT $2.25 + $1.15 + $0.02 = $3.42
  (IBKR prices crypto micros higher). Stated tolerance: ±$0.10/side (exchange
  fee schedules move; sources: interactivebrokers.com commissions-futures +
  CME fee schedule via tradestation.com exchange-fee table, 2026-06).
  LEAN's bundled ``InteractiveBrokersFeeModel`` (probe-measured: $0.57 for ALL
  CME/COMEX micros, $4.77 for MBT) understates MGC ~58% and overstates MBT
  ~39%, which is why the research path sets this table explicitly.
- ETFs use IBKR fixed per-share pricing: $0.005/share, $1.00 minimum per
  order — LEAN's bundled model matches this exactly (probe-measured), so the
  engine model is kept for ETFs and the numbers here are documentation.
- ``slippage_ticks``: adverse ticks applied per fill in the research cost
  model. Futures = 1 (the empirical fill is the same-session close — the very
  bar the signal was computed from — while the live ceremony dispatches at
  ~17:30 ET and fills at the 18:00 ET reopen; one tick is the explicit,
  conservative stand-in for that gap + half-spread). ETFs = 0 (fills land at
  the NEXT session's official open, an achievable auction print; TLT/IEF/SHY
  half-spread ≈ 1¢ ≈ ~1bp — documented as zero rather than hidden).

Fill conventions (probe-measured in this image, 2026-06-10; see
``FILL_CONVENTION``): a 17:30 ET scheduled futures market order fills
SAME-session at that session's close; an ETF market order pends overnight and
fills at the NEXT session's official open.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

AssetClass = Literal["future", "etf"]

#: When the commission/slippage reference numbers were last verified (PR B).
COSTS_AS_OF: Final = "2026-06"

#: Stated tolerance for "matches IBKR reality" (per contract per side, USD).
COSTS_TOLERANCE_PER_SIDE: Final = Decimal("0.10")

#: IBKR fixed per-share pricing for US ETFs (matches LEAN's bundled model).
ETF_COMMISSION_PER_SHARE: Final = Decimal("0.005")
ETF_COMMISSION_MIN_PER_ORDER: Final = Decimal("1.00")

#: Probe-measured fill conventions in the trading-lean-local image (PR B).
FILL_CONVENTION: Final[dict[AssetClass, str]] = {
    "future": "same-session close (17:30 ET order fills at that session's settlement)",
    "etf": "next-session official open (order pends overnight)",
}


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
    #: Futures: ALL-IN per-contract per-side cost (IBKR fixed + exchange + NFA;
    #: module docstring has the breakdown + as-of + tolerance). ETFs: ``None``
    #: (per-share pricing — ``ETF_COMMISSION_PER_SHARE`` / ``_MIN_PER_ORDER``).
    commission_per_side: Decimal | None = None
    #: Adverse ticks applied per fill by the research cost model (PR B).
    slippage_ticks: int = 0

    @property
    def dollars_per_tick(self) -> Decimal:
        """USD value of one tick for one contract (``multiplier * tick_size``)."""
        return self.multiplier * self.tick_size

    def notional(self, contracts: int, price: Decimal) -> Decimal:
        """Gross notional of ``contracts`` at ``price`` (always ``Decimal``)."""
        return Decimal(contracts) * self.multiplier * price

    def commission(self, quantity: int) -> Decimal:
        """One-side commission for ``abs(quantity)`` contracts/shares (``Decimal``).

        Futures: ``|quantity| * commission_per_side``. ETFs: IBKR fixed
        per-share pricing with the per-order minimum.
        """
        size = abs(quantity)
        if self.asset_class == "future":
            if self.commission_per_side is None:  # pragma: no cover — table invariant
                raise ValueError(f"{self.symbol} is a future without commission_per_side")
            return Decimal(size) * self.commission_per_side
        return max(Decimal(size) * ETF_COMMISSION_PER_SHARE, ETF_COMMISSION_MIN_PER_ORDER)

    @property
    def slippage_per_side(self) -> Decimal:
        """USD slippage cost per contract per side (``slippage_ticks`` ticks)."""
        return self.dollars_per_tick * self.slippage_ticks


_SPECS: Final[tuple[ContractSpec, ...]] = (
    # --- CME / CBOT / COMEX micro futures ---------------------------------
    # commission_per_side: IBKR $0.25 + exchange + $0.02 NFA (docstring).
    ContractSpec(
        "/MES",
        "E-mini S&P 500 Micro",
        "future",
        Decimal("5"),
        Decimal("0.25"),
        "USD",
        "cme",
        commission_per_side=Decimal("0.62"),
        slippage_ticks=1,
    ),
    ContractSpec(
        "/MNQ",
        "E-mini Nasdaq-100 Micro",
        "future",
        Decimal("2"),
        Decimal("0.25"),
        "USD",
        "cme",
        commission_per_side=Decimal("0.62"),
        slippage_ticks=1,
    ),
    ContractSpec(
        "/MYM",
        "E-mini Dow Micro",
        "future",
        Decimal("0.5"),
        Decimal("1.0"),
        "USD",
        "cbot",
        commission_per_side=Decimal("0.62"),
        slippage_ticks=1,
    ),
    ContractSpec(
        "/M2K",
        "E-mini Russell 2000 Micro",
        "future",
        Decimal("5"),
        Decimal("0.1"),
        "USD",
        "cme",
        commission_per_side=Decimal("0.62"),
        slippage_ticks=1,
    ),
    ContractSpec(
        "/MGC",
        "Gold Micro",
        "future",
        Decimal("10"),
        Decimal("0.1"),
        "USD",
        "comex",
        commission_per_side=Decimal("1.37"),
        slippage_ticks=1,
    ),
    ContractSpec(
        "/MBT",
        "Bitcoin Micro",
        "future",
        Decimal("0.1"),
        Decimal("5.0"),
        "USD",
        "cme",
        commission_per_side=Decimal("3.42"),
        slippage_ticks=1,
    ),
    # --- NYSE bond ETFs (cash equity; no contract roll) -------------------
    # ETF commissions are per-share (module constants); slippage 0 — fills land
    # at the next session's official open (module docstring).
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
