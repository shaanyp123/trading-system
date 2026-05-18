"""services/execution package — IBKR client + order placement.

Post-pivot 2026-05-12 (Pivot-PR-B). Replaces the pre-pivot QC ObjectStore
instruction protocol (backend-spec §4.5.2 RETIRED) with direct `ib-async`
calls to a Dockerized `ib_gateway` container.

**Public surface:**

* :class:`IbkrClient` (Protocol) — abstract interface in
  ``ibkr_client.py``. Programs against this; concrete impl in
  ``ibkr_adapter.py``.
* :class:`IbAsyncIbkrClient` — `ib-async`-backed adapter. Single
  instance per process; instantiated at startup with sops-sourced config.
* Pure-policy dataclasses in ``types.py``:
  :class:`IbkrPlaceOrderRequest`, :class:`IbkrPlaceOrderResult`,
  :class:`IbkrFill`, :class:`IbkrPosition`, :class:`IbkrAccountSummary`,
  :class:`IbkrContractRef`, :class:`IbkrConnectionState`,
  :class:`IbkrCancelOrderResult`, :class:`IbkrPlacementError`.

**A02 BINDS** — every file in this package is on the forbidden whitelist;
`risk-review-approved` required for any change.

**A13 (revised 2026-05-12)** — this package IS the canonical direct-IBKR
path. The pre-pivot rule (no direct IBKR in Phase 1) was inverted; see
``Docs/claude-dev-guide.md`` §11 [A13] revised + ``Docs/decisions-log.md``
2026-05-12 entry.

**A27** BINDS — operator runbook + futures-sim precondition smoke at
``deploy/ibkr/README.md``. The smoke (placeOrder + cancelOrder round-trip
on /MES paper) is a pre-deploy gate.

**Architectural integration:**

* The api process holds a single :class:`IbAsyncIbkrClient` instance,
  constructed at lifespan startup with config from
  ``services.api.config.APISettings`` (Pivot-PR-D wires this).
* Pivot-PR-D's ``services/risk/signal_dispatch.py`` consumes
  approved signals + calls into this client's ``place_order()``.
* Pivot-PR-C's ``services/reconciliation/`` uses ``get_positions()``
  + ``get_account_summary()`` on 60s intraday cadence during CME session.
"""

from __future__ import annotations

from services.execution.ibkr_adapter import (
    DEFAULT_CLIENT_ID,
    IBKR_CONNECTIVITY_ERROR_CODES,
    IBKR_DATA_FARM_ERROR_CODES,
    IbAsyncIbkrClient,
    IbkrErrorState,
    IbkrErrorStateProvider,
)
from services.execution.ibkr_client import IbkrClient
from services.execution.types import (
    IbkrAccountSummary,
    IbkrCancelOrderResult,
    IbkrConnectionState,
    IbkrContractRef,
    IbkrFill,
    IbkrOrderSide,
    IbkrOrderStatus,
    IbkrOrderType,
    IbkrPlacementError,
    IbkrPlaceOrderRequest,
    IbkrPlaceOrderResult,
    IbkrPosition,
    IbkrRejectionCategory,
)

__all__ = [
    "DEFAULT_CLIENT_ID",
    "IBKR_CONNECTIVITY_ERROR_CODES",
    "IBKR_DATA_FARM_ERROR_CODES",
    "IbAsyncIbkrClient",
    "IbkrAccountSummary",
    "IbkrCancelOrderResult",
    "IbkrClient",
    "IbkrConnectionState",
    "IbkrContractRef",
    "IbkrErrorState",
    "IbkrErrorStateProvider",
    "IbkrFill",
    "IbkrOrderSide",
    "IbkrOrderStatus",
    "IbkrOrderType",
    "IbkrPlaceOrderRequest",
    "IbkrPlaceOrderResult",
    "IbkrPlacementError",
    "IbkrPosition",
    "IbkrRejectionCategory",
]
