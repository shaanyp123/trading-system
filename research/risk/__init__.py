"""``research.risk`` — leverage / margin / liquidation / ruin modeling (P3).

The point of this package is to make leverage **un-missable**: it shows the
leverage at which the strategy would have been LIQUIDATED and how often, rather
than flattering a backtest that silently "rode through" a wipeout (design §6.2).

Modules:

* :mod:`research.risk.sizing_schemes` — fixed / fixed-fractional / vol-target /
  ATR-based / risk-parity sizing, each a pure function under a HARD leverage cap.
* :mod:`research.risk.leverage` — per-bar gross leverage (peak/mean/terminal) +
  the hard-cap clamp the sizing schemes enforce.
* :mod:`research.risk.liquidation` — the daily-bar intrabar liquidation ESTIMATOR
  (§6.3): a flag-for-confirmation WARNING with explicit residual uncertainty,
  never a verdict.
* :mod:`research.risk.metrics` — the full §6.5 ruin suite (moved + extended from
  ``research.eval.metrics``), replacing P1's post-wipeout max-drawdown limitation
  with liquidation-aware ruin metrics.

Float analytics under the design-D8 exemption (numpy/vbt are float). Money-
adjacent contract specs stay ``Decimal`` (``research.data.contract_specs``); every
value crossing a governance boundary — the vol-target parity pin against the live
``services/risk/sizing.py`` Stage-1 vector — is re-materialized as ``Decimal``
there (§13 / R7).
"""

from __future__ import annotations
