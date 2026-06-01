"""services/api/bar_sync_status.py — process-local handle on the BarSyncWorker.

Bridges the lifespan-owned :class:`services.data.bar_sync.BarSyncWorker`
to the read-only ``GET /api/system/bar-sync`` status surface so the
operator can answer "how did the last bar_sync cycle go?" without SSHing
to the VPS to grep journald.

**Why a thin provider instead of ``app.state`` or a direct import:**
  * The worker is constructed inside the api lifespan (``_start_bar_sync_worker``)
    and is otherwise scoped to that context manager — route handlers can't
    reach it. A module-level provider, set during lifespan, is the same
    pattern the heartbeat registry uses (``services/api/heartbeats.py``).
  * The route module must NOT import ``services.data.bar_sync`` at import
    time (it lazy-loads ib-async + pulls the whole data subpackage). The
    provider holds the worker behind the small :class:`BarSyncStatusSource`
    Protocol so the route depends only on the read shape, not the impl.

**Why in-memory (no DB table):** identical rationale to the heartbeat
registry — cycle outcomes fire once daily; persisting them per cycle would
add an alembic migration (a forbidden path requiring the risk-review
label) for no real gain. The worker retains its last
:class:`~services.data.bar_sync.BarSyncCycleResult` in memory; on api
restart the provider reads ``None`` until the next cycle, which the
endpoint reports honestly as "no cycle since restart". No alerts, no new
event types — a pure read surface.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover — type-only
    from services.data.bar_sync import BarSyncCycleResult


@runtime_checkable
class BarSyncStatusSource(Protocol):
    """Read-only view the status endpoint needs from a BarSyncWorker.

    :class:`services.data.bar_sync.BarSyncWorker` satisfies this
    structurally via its public properties; declaring it as a Protocol
    keeps the route + provider decoupled from the worker's full surface
    (and from the ib-async import graph it lazy-loads).
    """

    @property
    def last_cycle_result(self) -> BarSyncCycleResult | None: ...

    @property
    def consecutive_failure_count(self) -> int: ...

    @property
    def consecutive_sentinel_count(self) -> int: ...

    @property
    def last_fired_session_date_et(self) -> date | None: ...

    @property
    def is_running(self) -> bool: ...


class BarSyncStatusProvider:
    """Process-local holder for the current :class:`BarSyncStatusSource`.

    The api lifespan calls :meth:`set_source` once the worker is
    constructed and :meth:`clear` on shutdown. The route reads
    :meth:`get_source` (``None`` when the worker isn't running — e.g.
    ``bar_sync_enabled=False`` or a startup failure — which the endpoint
    renders as ``status="pending"``).
    """

    __slots__ = ("_source",)

    def __init__(self) -> None:
        self._source: BarSyncStatusSource | None = None

    def set_source(self, source: BarSyncStatusSource) -> None:
        """Register the active worker handle (called from the api lifespan)."""
        self._source = source

    def clear(self) -> None:
        """Drop the handle (api shutdown, or test teardown)."""
        self._source = None

    def get_source(self) -> BarSyncStatusSource | None:
        """Return the active worker handle, or None if none is registered."""
        return self._source


#: Module-level singleton. Convention mirrors
#: :data:`services.api.heartbeats._REGISTRY` — one process-wide instance,
#: retrieved via :func:`get_bar_sync_status_provider`.
_PROVIDER = BarSyncStatusProvider()


def get_bar_sync_status_provider() -> BarSyncStatusProvider:
    """Return the process-wide singleton provider."""
    return _PROVIDER


__all__ = [
    "BarSyncStatusProvider",
    "BarSyncStatusSource",
    "get_bar_sync_status_provider",
]
