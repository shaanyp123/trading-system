"""services/data — data-pipeline modules.

Currently hosts the ``bar_sync`` worker (Option C of the 2026-05-20
data-layer pivot v2; see ``Docs/decisions-log.md`` 2026-05-20 evening
entry). The worker fetches daily OHLCV bars for the Phase 1 universe
from IBKR via a dedicated ib-async connection on clientId=2 and
writes them to the shared ``lean_data`` Docker volume in LEAN's
expected on-disk format. LEAN reads via ``FakeDataQueue`` +
``SubscriptionDataReaderHistoryProvider`` (the original retired path
that this pivot revives, with api-managed freshness).

This package is NOT on the dev-guide §2.2 forbidden whitelist;
regular PR review applies (no ``risk-review-approved`` label required).
"""
