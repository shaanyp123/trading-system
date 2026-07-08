"""services/execution — broker execution layer (Coinbase CFM, crypto-pivot C0-B2b).

Layered per delta spec §3.1:

* ``types.py`` — venue-neutral DTOs (orders, fills, positions, balance).
* ``coinbase_client.py`` — :class:`CoinbaseBrokerClient` Protocol + the
  ``coinbase-advanced-py``-backed :class:`SdkCoinbaseBrokerClient`
  transport (JWT-authenticated REST; sync SDK bridged via
  ``asyncio.to_thread``).
* ``coinbase_adapter.py`` — :class:`CoinbaseExecutionAdapter` policy:
  the strategy-§5 execution ladder (post-only at touch → 10 min →
  mid ± 5 bps IOC → market), deterministic ``client_order_id`` crash
  idempotency, native 3xATR stop management with resting verification,
  startup reconcile, cancel-all.

The IBKR client/adapter that previously lived here (Pivot-PR-B,
2026-05-12 → 2026-07-08) was deleted in C0-B2b together with the
``ib-async`` dependency — see ``Docs/recent-architecture-changes.md``
(crypto-pivot entries) and [A13] revised: do NOT re-introduce IBKR
paths.

A02 BINDS — everything under ``services/execution/**`` requires the
``risk-review-approved`` PR label.
A27 — operator canary runbook at ``deploy/coinbase_execution/README.md``.
"""

from services.execution.coinbase_adapter import (
    INTRADAY_MARGIN_OPTIN_FORBIDDEN,
    CoinbaseExecutionAdapter,
    LadderExecutionResult,
    LadderStageResult,
    StartupSnapshot,
    StopEnsureResult,
    StopVerificationError,
    compute_stop_prices,
    deterministic_client_order_id,
    find_unprotected_positions,
    round_to_tick,
    select_perp_products,
)
from services.execution.coinbase_client import (
    CoinbaseBrokerClient,
    SdkCoinbaseBrokerClient,
)
from services.execution.types import (
    BestBidAsk,
    BrokerError,
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerPosition,
    FuturesBalanceSummary,
    OrderKind,
    OrderSide,
    PerpProductRef,
)

__all__ = [
    "INTRADAY_MARGIN_OPTIN_FORBIDDEN",
    "BestBidAsk",
    "BrokerError",
    "BrokerFill",
    "BrokerOrderAck",
    "BrokerOrderRequest",
    "BrokerOrderState",
    "BrokerPosition",
    "CoinbaseBrokerClient",
    "CoinbaseExecutionAdapter",
    "FuturesBalanceSummary",
    "LadderExecutionResult",
    "LadderStageResult",
    "OrderKind",
    "OrderSide",
    "PerpProductRef",
    "SdkCoinbaseBrokerClient",
    "StartupSnapshot",
    "StopEnsureResult",
    "StopVerificationError",
    "compute_stop_prices",
    "deterministic_client_order_id",
    "find_unprotected_positions",
    "round_to_tick",
    "select_perp_products",
]
