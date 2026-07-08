"""Unit tests for the risk-state dispatch gate (services/risk/signal_dispatch).

Crypto-pivot C0-B4: the per-trade approval dispatcher
(``plan_signal_{approve,reject,defer}`` + ``apply_signal_dispatch``)
was RETIRED with the announce-only mandate; these tests cover what
survives — the venue-agnostic risk-state gate the trading loop consults
before dispatching (``RISK_STATES_PERMITTING_DISPATCH`` +
``fetch_current_risk_state``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from services.risk.signal_dispatch import (
    RISK_STATES_PERMITTING_DISPATCH,
    fetch_current_risk_state,
)

_ACCOUNT_ID = uuid4()


class TestRiskStateGate:
    def test_locked_permit_set(self) -> None:
        """NORMAL + CONVALESCENT permit dispatch; HALT_NEW rejects
        (backend-spec §2.5 — unchanged by the crypto pivot)."""
        assert RISK_STATES_PERMITTING_DISPATCH == frozenset({"NORMAL", "CONVALESCENT"})
        assert "HALT_NEW" not in RISK_STATES_PERMITTING_DISPATCH


def _factory_returning(row: Any) -> Any:
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = row
    session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def _factory() -> AsyncIterator[Any]:
        yield session

    return _factory


class TestFetchCurrentRiskState:
    async def test_returns_state_when_row_present(self) -> None:
        row = MagicMock()
        row.state = "NORMAL"
        state = await fetch_current_risk_state(_factory_returning(row), account_id=_ACCOUNT_ID)
        assert state == "NORMAL"

    async def test_returns_none_when_no_row(self) -> None:
        state = await fetch_current_risk_state(_factory_returning(None), account_id=_ACCOUNT_ID)
        assert state is None

    async def test_returns_halt_new_when_halted(self) -> None:
        row = MagicMock()
        row.state = "HALT_NEW"
        state = await fetch_current_risk_state(_factory_returning(row), account_id=_ACCOUNT_ID)
        assert state == "HALT_NEW"
        assert state not in RISK_STATES_PERMITTING_DISPATCH
