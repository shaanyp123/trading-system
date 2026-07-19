"""Unit tests for ``POST /api/system/cash-capture`` (Amendment C re-capture).

Route-level coverage via the conftest ASGI app with the capture-job
dependency overridden (same pattern as ``test_api_gates_route.py``):

  * happy path — 200 with Decimal-as-string balances + replaced flag,
    and today's UTC date passed to the job
  * credentials missing (job dependency resolves None) → 503
  * venue read failure (job raises CashCaptureError) → 502
  * request-body validation (extra fields forbidden)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from httpx import AsyncClient

from services.api.routes import system as system_route
from services.data.usdc_rewards import CashCaptureError, RecaptureResult

_CSRF_TOKEN = "test-csrf-token-cash-capture"


def _csrf_kwargs() -> dict[str, Any]:
    """Double-submit CSRF pair the middleware requires on every POST."""
    return {
        "cookies": {"__Host-csrf_token": _CSRF_TOKEN},
        "headers": {"X-CSRF-Token": _CSRF_TOKEN},
    }


class _FakeCaptureJob:
    def __init__(self, *, raises: bool = False, replaced: bool = True) -> None:
        self.raises = raises
        self.replaced = replaced
        self.calls: list[date] = []

    async def run_recapture_once(self, session_date_utc: date) -> RecaptureResult:
        self.calls.append(session_date_utc)
        if self.raises:
            raise CashCaptureError(operation="get_accounts", detail="venue down")
        return RecaptureResult(
            snapshot_date_utc=session_date_utc,
            captured_at_utc=datetime(2026, 7, 19, 14, 30, tzinfo=UTC),
            spot_usd=Decimal("2251.38"),
            cbi_usdc=Decimal("3497.90"),
            replaced=self.replaced,
        )


def _wire(override_dep: Any, job: _FakeCaptureJob | None) -> None:
    override_dep(system_route._get_cash_capture_job, lambda: job)


class TestCashCaptureRoute:
    async def test_recapture_happy_path(self, api_client: AsyncClient, override_dep: Any) -> None:
        job = _FakeCaptureJob(replaced=True)
        _wire(override_dep, job)
        resp = await api_client.post(
            "/api/system/cash-capture",
            json={"reason": "converted 802.10 USDC->USD"},
            **_csrf_kwargs(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["spot_usd"] == "2251.38"
        assert body["cbi_usdc"] == "3497.90"
        assert body["replaced"] is True
        # The job is invoked for TODAY's UTC date (anchored to the real
        # clock — the route reads datetime.now()).
        assert job.calls == [datetime.now(tz=UTC).date()]

    async def test_first_capture_of_day_reports_not_replaced(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, _FakeCaptureJob(replaced=False))
        resp = await api_client.post("/api/system/cash-capture", json={}, **_csrf_kwargs())
        assert resp.status_code == 200
        assert resp.json()["replaced"] is False

    async def test_missing_credentials_503(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, None)
        resp = await api_client.post("/api/system/cash-capture", json={}, **_csrf_kwargs())
        assert resp.status_code == 503
        assert resp.json()["error_code"] == "CASH_CAPTURE_CREDENTIALS_MISSING"

    async def test_venue_failure_502(self, api_client: AsyncClient, override_dep: Any) -> None:
        _wire(override_dep, _FakeCaptureJob(raises=True))
        resp = await api_client.post("/api/system/cash-capture", json={}, **_csrf_kwargs())
        assert resp.status_code == 502
        assert resp.json()["error_code"] == "CASH_CAPTURE_VENUE_FAILURE"

    async def test_extra_body_fields_rejected(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, _FakeCaptureJob())
        resp = await api_client.post(
            "/api/system/cash-capture",
            json={"reason": "x", "overwrite": True},
            **_csrf_kwargs(),
        )
        assert resp.status_code == 422
