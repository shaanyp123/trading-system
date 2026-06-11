# research/lean/projects/research_runner.py — the GENERIC authoritative LEAN runner.
#
# One native QCAlgorithm that backtests a chosen reference strategy over a single
# instrument (a CONTINUOUS future or a cash ETF) at any resolution, with LEAN's
# real fills + IBKR margin + forced-liquidation model. It is the authoritative
# engine for `make research ... engine: lean` (research/run.py routes here for the
# reference strategies; production V1 goes via lean/v1_strategy.py, which POSTs).
#
# Like donchian_reference.py this is a self-contained, NON-POSTING algorithm: it
# reads everything from the LEAN `parameters` block (rendered by the research
# driver) and never reaches an api. It is excluded from ruff + mypy (pyproject
# `research/lean/projects`) because `from AlgorithmImports import *` only resolves
# inside LEAN's runtime.
#
# DESIGN CHOICE — parity-gated TWIN, not a numpy import. The strategy signal logic
# is reimplemented inline here (mirroring research/strategy/*.py) rather than
# importing the numpy ResearchStrategy into the container. That is the established
# pattern (donchian_reference.py is the LEAN twin of donchian.py, kept honest by
# tests/integration/test_vbt_lean_parity.py); the §6.6 parity rail — not literal
# code sharing — is what guarantees the screen and the authority agree. It also
# avoids mounting research/+strategies/ onto the container path (the fragility that
# dogged the earlier strategy-runner attempt). Each strategy added here gets its
# own inline expression + a parity check.
#
# Three continuous-futures realities this handles (no in-repo template existed —
# v1_strategy.py POSTs and places NO LEAN orders, so it never solved order
# placement; donchian_reference.py is equity-only):
#   (G1) size in INTEGER CONTRACTS via market_order — set_holdings rejects a
#        futures target whose |weight| > 1 (a single micro is a small notional, so
#        a fixed weight would over/under-size and error).
#   (G2) trade the MAPPED front contract, never the continuous canonical — orders
#        on the canonical symbol do nothing; positions live in the dated contract.
#   (G3) on a roll, the front contract changes: close the expiring leg and let the
#        reconcile step re-open the target on the new front. The mapped contract's
#        price can be 0 before it has a bar (early chain edge) — skip ordering then.

from collections import deque

from AlgorithmImports import *  # noqa: F403

_RESOLUTIONS = {
    "daily": Resolution.DAILY,  # noqa: F405
    "hour": Resolution.HOUR,  # noqa: F405
    "minute": Resolution.MINUTE,  # noqa: F405
    "tick": Resolution.TICK,  # noqa: F405
}

# --- explicit cost model (charter PR B) -------------------------------------
# ALL-IN per-contract per-side commission (IBKR fixed + exchange + NFA, as of
# 2026-06) and tick sizes for 1-tick adverse slippage. These mirror
# research/data/contract_specs.py (the canonical table — a unit test pins the
# two in sync; this file cannot import research.* inside the LEAN container).
# LEAN's bundled InteractiveBrokersFeeModel is stale (probe-measured: $0.57 for
# ALL CME/COMEX micros, $4.77 for MBT vs reality $0.62 / $1.37 / $3.42), which
# is why COSTS_MODEL=ibkr sets fees explicitly. ETFs keep LEAN's bundled model
# ($0.005/share min $1.00 — probe-measured to match IBKR fixed exactly).
_FUTURES_COMMISSION_PER_SIDE = {
    "MES": 0.62,
    "MNQ": 0.62,
    "MYM": 0.62,
    "M2K": 0.62,
    "MGC": 1.37,
    "MBT": 3.42,
}
_FUTURES_TICK_SIZE = {
    "MES": 0.25,
    "MNQ": 0.25,
    "MYM": 1.0,
    "M2K": 0.1,
    "MGC": 0.1,
    "MBT": 5.0,
}
#: Adverse ticks per futures fill (explicit, reported; ETFs are 0 — they fill
#: at the next session's official open, an achievable auction print).
_FUTURES_SLIPPAGE_TICKS = 1


class PerContractFeeModel(FeeModel):  # noqa: F405
    """Explicit all-in per-contract per-side commission (research cost model)."""

    def __init__(self, fee_per_contract):
        super().__init__()
        self._fee_per_contract = fee_per_contract

    def get_order_fee(self, parameters):
        quantity = abs(parameters.order.quantity)
        return OrderFee(CashAmount(quantity * self._fee_per_contract, "USD"))  # noqa: F405


class TickSlippageModel:
    """N ticks of adverse slippage per fill, in absolute price units.

    LEAN duck-types slippage models (``get_slippage_approximation``); the fill
    model applies the returned price offset AGAINST the trade direction.
    """

    def __init__(self, tick_size, ticks):
        self._slippage = tick_size * ticks

    def get_slippage_approximation(self, asset, order):
        return self._slippage


def _parse_yyyymmdd(token, fallback):
    token = (token or "").strip()
    if len(token) != 8 or not token.isdigit():
        token = fallback
    return int(token[:4]), int(token[4:6]), int(token[6:8])


class ResearchRunnerAlgorithm(QCAlgorithm):  # noqa: F405
    """Generic single-instrument runner — futures (continuous) or cash ETF.

    Parameters (all strings; the research driver renders them):
      RESEARCH_SYMBOL    "/MES" (future, leading slash) | "TLT" (ETF, bare)
      STRATEGY_REF       "donchian" | "buy_and_hold"
      DONCHIAN_CHANNEL   int, donchian lookback (default 20)
      CONTRACTS          int, position size in integer contracts (default 1)
      START_DATE/END_DATE  YYYYMMDD
      STARTING_CASH_USD  int
      RESOLUTION         daily|hour|minute|tick (default daily — intraday deferred,
                         but the engine is resolution-agnostic so the knob exists)
      COSTS_MODEL        "ibkr" (default, realistic) | "zero" (clean comparison)
    """

    def initialize(self):
        symbol_param = (self.get_parameter("RESEARCH_SYMBOL") or "TLT").strip()
        self._strategy_ref = (self.get_parameter("STRATEGY_REF") or "buy_and_hold").strip().lower()
        self._channel = int(self.get_parameter("DONCHIAN_CHANNEL") or 20)
        self._contracts = int(self.get_parameter("CONTRACTS") or 1)
        cash = int(self.get_parameter("STARTING_CASH_USD") or 100000)
        costs_model = (self.get_parameter("COSTS_MODEL") or "ibkr").strip().lower()
        resolution = _RESOLUTIONS.get(
            (self.get_parameter("RESOLUTION") or "daily").strip().lower(),
            Resolution.DAILY,  # noqa: F405
        )

        start_y, start_m, start_d = _parse_yyyymmdd(self.get_parameter("START_DATE"), "20240101")
        end_y, end_m, end_d = _parse_yyyymmdd(self.get_parameter("END_DATE"), "20251231")
        self.set_start_date(start_y, start_m, start_d)
        self.set_end_date(end_y, end_m, end_d)
        self.set_cash(cash)
        self.set_time_zone("America/New_York")

        # IBKR MARGIN model = LEAN-native maintenance margin + forced liquidation.
        # This is the whole point of the authoritative path (design §6.2): a margin
        # call must be able to fire and be reported, not assumed away. Mirrors
        # lean/v1_strategy.py:298.
        self.set_brokerage_model(
            BrokerageName.INTERACTIVE_BROKERS_BROKERAGE,  # noqa: F405
            AccountType.MARGIN,  # noqa: F405
        )

        self._is_future = symbol_param.startswith("/")
        if self._is_future:
            # Bare ticker — add_future prepends the slash itself (passing "/MES"
            # yields a double-slash canonical that misses the map_file lookup →
            # MapFile.Count 0). RAW + OPEN_INTEREST + contract_depth_offset=0 +
            # set_filter(-365, 90) exactly mirrors lean/v1_strategy.py:370 so the
            # continuous-contract stitching matches production (and the §6.6 parity
            # assumptions hold). RAW avoids a factor_file dependency the on-disk
            # COPY does not carry.
            ticker = symbol_param.lstrip("/").upper()
            future = self.add_future(  # noqa: F405
                ticker,
                resolution=resolution,
                extended_market_hours=False,
                data_mapping_mode=DataMappingMode.OPEN_INTEREST,  # noqa: F405
                data_normalization_mode=DataNormalizationMode.RAW,  # noqa: F405
                contract_depth_offset=0,
            )
            future.set_filter(-365, 90)
            self._symbol = future.symbol  # the CONTINUOUS canonical (e.g. /MES)
            self._security = future
        else:
            equity = self.add_equity(  # noqa: F405
                symbol_param,
                resolution=resolution,
                extended_market_hours=False,
                data_normalization_mode=DataNormalizationMode.RAW,  # noqa: F405
            )
            self._symbol = equity.symbol
            self._security = equity

        # Cost models are applied PER TRADED SECURITY via _ensure_cost_models:
        # futures fills land on the MAPPED contract securities (G2), so a model
        # set on the canonical alone never touches a fill — the pre-PR-B "zero"
        # path had exactly that gap. The primary security is costed here (the
        # whole story for an ETF; harmless for a future's canonical) and every
        # mapped contract is costed before it is ordered.
        self._costs_model = costs_model
        self._costed_symbols = set()
        self._ensure_cost_models(self._symbol)

        self._highs = deque(maxlen=self._channel)
        self._lows = deque(maxlen=self._channel)
        self._target = 0  # desired position in integer contracts (signed)

        # Warmup: donchian needs `channel` prior bars (+1 so the window is full on
        # the first tradeable bar); buy-and-hold needs none.
        warmup = self._channel + 1 if self._strategy_ref == "donchian" else 1
        self.set_warm_up(warmup, resolution)

        self.log(
            "research_runner initialized "
            f"symbol={symbol_param} strategy={self._strategy_ref} "
            f"channel={self._channel} contracts={self._contracts} cash={cash} "
            f"resolution={resolution} costs={costs_model} future={self._is_future} "
            f"window=[{start_y}{start_m:02d}{start_d:02d},{end_y}{end_m:02d}{end_d:02d}]"
        )

    def _mapped_symbol(self):
        """The contract to trade: the mapped front for a future, else the symbol (G2)."""
        if not self._is_future:
            return self._symbol
        return self.securities[self._symbol].mapped  # may be None before first map

    def _ensure_cost_models(self, symbol) -> None:
        """Apply the configured cost model to ``symbol``'s security, once.

        Called for the primary security at initialize and for every (mapped)
        contract before it is ordered — fee/slippage models live on the
        SECURITY that fills, so each traded contract needs its own application.

          zero  -> no fees, no slippage on everything (clean numpy comparison).
          ibkr  -> futures: explicit per-contract commission + 1-tick adverse
                   slippage (tables above); ETFs: keep LEAN's bundled IBKR
                   equity model (probe-verified accurate) + no slippage (fills
                   at the next session's official open).
        """
        if symbol is None or symbol in self._costed_symbols or symbol not in self.securities:
            return
        security = self.securities[symbol]
        if self._costs_model == "zero":
            security.set_fee_model(ConstantFeeModel(0))  # noqa: F405
            security.set_slippage_model(ConstantSlippageModel(0))  # noqa: F405
        elif self._is_future:
            ticker = str(symbol.id.symbol).upper()
            fee = _FUTURES_COMMISSION_PER_SIDE.get(ticker)
            tick = _FUTURES_TICK_SIZE.get(ticker)
            if fee is not None:
                security.set_fee_model(PerContractFeeModel(fee))
            if tick is not None:
                security.set_slippage_model(
                    TickSlippageModel(tick, _FUTURES_SLIPPAGE_TICKS)
                )
        self._costed_symbols.add(symbol)

    def _decide_target(self, bar) -> int:
        """Return the desired integer-contract position from the strategy (long side).

        Mirrors the numpy ResearchStrategy twins (donchian.py / buy_and_hold.py):
        the donchian channel is built from bars STRICTLY BEFORE the current one
        (the window is pushed after this call), compared to the current close.
        """
        if self._strategy_ref == "buy_and_hold":
            return self._contracts
        if self._strategy_ref == "donchian":
            if len(self._highs) < self._channel:
                return self._target  # window not full yet — hold
            upper = max(self._highs)
            lower = min(self._lows)
            if self._target == 0 and bar.close > upper:
                return self._contracts
            if self._target != 0 and bar.close < lower:
                return 0
            return self._target
        raise ValueError(f"unknown STRATEGY_REF {self._strategy_ref!r}")

    def on_symbol_changed_events(self, events):  # noqa: F405
        """Roll handling (G3): close the expiring leg; reconcile re-opens the target.

        LEAN routes continuous-future rolls here. We flatten the OLD contract; the
        next on_data reconcile buys ``_target`` on the new front, so the position
        carries across the roll without per-contract bookkeeping.

        ⚠ Close with a plain ``market_order(..., tag="roll")`` — NOT ``self.liquidate()``,
        whose "Liquidated" order tag would trip ``parse_margin_events`` (it matches
        "liquidat") and raise a FALSE forced-liquidation banner. A roll is an ordinary
        position carry, not a margin event.
        """
        for changed in events.values():
            old = changed.old_symbol
            quantity = self.portfolio[old].quantity
            if quantity != 0:
                # ``old`` is a string in this build; resolve the Symbol via the
                # security so the cost-model application keys consistently.
                if old in self.securities:
                    self._ensure_cost_models(self.securities[old].symbol)
                self.market_order(old, -quantity, tag="roll")

    def on_data(self, data):  # noqa: F405
        bar = data.bars.get(self._symbol) if data.bars is not None else None
        if bar is None:
            return

        if not self.is_warming_up:
            self._target = self._decide_target(bar)
            mapped = self._mapped_symbol()
            if mapped is not None and mapped in self.securities:
                price = self.securities[mapped].price
                if price and price > 0:  # G3: mapped price can be 0 on the chain edge
                    current = self.portfolio[mapped].quantity
                    delta = self._target - current
                    if delta != 0:
                        self._ensure_cost_models(mapped)  # fills land on the contract
                        self.market_order(mapped, delta)  # G1: integer contracts

        # Push AFTER the decision so the donchian channel always excludes today.
        self._highs.append(bar.high)
        self._lows.append(bar.low)
