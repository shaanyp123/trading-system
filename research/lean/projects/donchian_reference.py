# research/lean/projects/donchian_reference.py — LEAN side of the §6.6 parity rail.
#
# A self-contained, NON-POSTING reference algorithm. Unlike lean/v1_strategy.py it
# does NOT POST to any api and does NOT read os.environ for api config — so it is
# the safe choice for the parser's captured fixture and for the parity rail (no
# POST path is ever exercised). It reads its window / cash / channel / symbol from
# the LEAN `parameters` block (rendered by research/lean/config_render.py), so the
# research DRIVER, not this file, owns the backtest window.
#
# Rules MUST match research/strategy/donchian.py byte-for-byte in intent (long-only
# Donchian breakout): enter `CONTRACTS` long when close breaks above the upper
# channel = max(high over the prior `DONCHIAN_CHANNEL` bars); exit flat when close
# breaks below the lower channel = min(low over the prior `DONCHIAN_CHANNEL` bars).
# The rolling window holds COMPLETED bars and the current bar's close is compared
# BEFORE the current bar is pushed — i.e. the channel excludes today, exactly like
# the numpy version's `high[t-channel:t]`.
#
# Fees + slippage are zeroed so the ONLY LEAN-vs-numpy divergence is the fill
# convention (LEAN fills the market order at the next bar's open; the numpy screen
# is close-to-close). The §6.6 tolerances (per-trade slippage <= 5bps, aggregate
# P&L <= 0.5% of starting equity, trade count within 5%) cover exactly that gap.
#
# This file is excluded from ruff + mypy (pyproject `research/lean/projects`) like
# lean/ — `from AlgorithmImports import *` only resolves inside LEAN's runtime.

from collections import deque

from AlgorithmImports import *  # noqa: F403


def _parse_yyyymmdd(token, fallback):
    token = (token or "").strip()
    if len(token) != 8 or not token.isdigit():
        token = fallback
    return int(token[:4]), int(token[4:6]), int(token[6:8])


class DonchianReferenceAlgorithm(QCAlgorithm):  # noqa: F405
    """Long-only Donchian breakout reference (parity twin of DonchianBreakout)."""

    def initialize(self):
        self._channel = int(self.get_parameter("DONCHIAN_CHANNEL") or 20)
        self._contracts = int(self.get_parameter("CONTRACTS") or 1)
        ticker = (self.get_parameter("RESEARCH_SYMBOL") or "TLT").strip()
        cash = int(self.get_parameter("STARTING_CASH_USD") or 100000)

        start_y, start_m, start_d = _parse_yyyymmdd(self.get_parameter("START_DATE"), "20240101")
        end_y, end_m, end_d = _parse_yyyymmdd(self.get_parameter("END_DATE"), "20251231")
        self.set_start_date(start_y, start_m, start_d)
        self.set_end_date(end_y, end_m, end_d)
        self.set_cash(cash)
        self.set_time_zone("America/New_York")

        # RAW normalization to match the numpy loader's raw close-to-close prices
        # (research/data/daily_loader.py) AND to avoid a factor_file dependency the
        # on-disk bar COPY does not carry. Default add_equity is Adjusted.
        equity = self.add_equity(  # noqa: F405
            ticker,
            resolution=Resolution.DAILY,  # noqa: F405
            extended_market_hours=False,
            data_normalization_mode=DataNormalizationMode.RAW,  # noqa: F405
        )
        self._symbol = equity.symbol
        # Zero fees + slippage so parity isolates the fill-convention gap (above).
        equity.set_fee_model(ConstantFeeModel(0))  # noqa: F405
        equity.set_slippage_model(ConstantSlippageModel(0))  # noqa: F405

        self._highs = deque(maxlen=self._channel)
        self._lows = deque(maxlen=self._channel)
        # +1 so the window is full on the first tradeable bar.
        self.set_warm_up(self._channel + 1, Resolution.DAILY)  # noqa: F405

        self.log(
            "donchian_reference initialized "
            f"symbol={ticker} channel={self._channel} contracts={self._contracts} "
            f"cash={cash} window=[{start_y}-{start_m}-{start_d},{end_y}-{end_m}-{end_d}]"
        )

    def on_data(self, data):  # noqa: F405
        bar = data.bars.get(self._symbol) if data.bars is not None else None
        if bar is None:
            return
        window_full = len(self._highs) == self._channel
        if not self.is_warming_up and window_full:
            upper = max(self._highs)
            lower = min(self._lows)
            invested = self.portfolio[self._symbol].invested
            if not invested and bar.close > upper:
                self.market_order(self._symbol, self._contracts)
            elif invested and bar.close < lower:
                self.liquidate(self._symbol)
        # Push AFTER the decision so the channel always excludes the current bar.
        self._highs.append(bar.high)
        self._lows.append(bar.low)
