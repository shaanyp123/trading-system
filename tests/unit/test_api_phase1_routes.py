"""Unit tests for the Day 15 / Week 5 Mon Phase-1 REST scaffold.

Coverage matrix (mirrors backend-spec §4.1 / IG §3 Week 5 Mon list):

  * §4.1.1 ``GET /api/auth/me``              — 1 happy + 1 schema-shape.
  * §4.1.2 ``GET /api/signals``              — 1 happy (empty list)
                                                + 1 status-filter passthrough.
  * §4.1.2 ``POST /api/signals/:id/approve`` — 1 happy (501 stub)
                                                + 1 schema-validation reject.
  * §4.1.2 ``POST /api/signals/:id/reject``  — 1 happy (501 stub)
                                                + 1 missing-diary 422.
  * §4.1.2 ``POST /api/signals/:id/defer``   — 1 happy (501 stub).
  * §4.1.3 ``GET /api/system/status``        — 1 happy (no-account fallback)
                                                + 1 with-row populated.
  * §4.1.3 ``GET /api/system/kill-switch``   — 1 happy (no-account fallback).
  * §4.1.3 ``POST /api/system/kill-switch/invoke`` — 1 happy (501 stub)
                                                       + 1 invalid-trigger 422.
  * §4.1.3 ``POST /api/system/kill-switch/resume`` — 1 happy (501 stub)
                                                       + 1 schema-shape.
  * §4.1.5b ``GET /api/health-score``        — 1 happy (insufficient_data).
  * §4.1.5b ``GET /api/today/digest``        — 1 happy (empty envelope).
  * §4.1.5b ``GET /api/positions/current``   — 1 happy (empty list).
  * §4.1.5b ``GET /api/orders``              — 1 happy (empty list)
                                                + 1 limit-clamp.
  * §4.1.5b ``GET /api/fills``               — 1 happy (empty list).
  * §4.1.5b ``GET /api/alerts``              — 1 happy (empty list)
                                                + 1 status+severity filter.
  * Session stub                              — 1 stub-injected on protected
                                                endpoint
                                                + 1 exempt-path bypass.
  * Cursor pagination                         — 1 encode/decode round-trip
                                                + 1 invalid-cursor envelope.

Each route gets a stub Phase1QueryRepo via ``override_dep`` (the helper
defined in ``tests/unit/conftest.py``); the repo returns canned no-data /
populated rows so the route's mapper code is exercised without testcontainers.

The Day 5 ``test_api_*.py`` modules already exercise ``health`` + ``setup`` +
``sse`` + middleware integration; this file only covers Day 15 surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from services.api.repos.phase1 import (
    AlertCountRow,
    AuditLogRow,
    ParameterSetRow,
    Phase1QueryRepo,
    ReconciliationSummaryRow,
    RiskStateRow,
)
from services.api.routes import (
    alerts as alerts_route,
)
from services.api.routes import (
    fills as fills_route,
)
from services.api.routes import (
    orders as orders_route,
)
from services.api.routes import (
    positions as positions_route,
)
from services.api.routes import (
    signals as signals_route,
)
from services.api.routes import (
    system as system_route,
)
from services.api.routes import (
    today as today_route,
)
from services.api.routes import (
    trades as trades_route,
)
from services.api.routes._pagination import (
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

# ---------------------------------------------------------------------------
# Stub repo
# ---------------------------------------------------------------------------


@dataclass
class _StubRepo:
    """In-memory ``Phase1QueryRepo`` for unit tests."""

    account_id: UUID | None = None
    risk_row: RiskStateRow | None = None
    recon_row: ReconciliationSummaryRow | None = None
    watchdog_ping: datetime | None = None
    pending_signals: int = 0
    alert_counts: AlertCountRow | None = None
    signals_page: tuple[list[dict[str, Any]], str | None, bool] = (
        [],
        None,
        False,
    )
    positions_rows: list[dict[str, Any]] = None  # type: ignore[assignment]
    orders_page: tuple[list[dict[str, Any]], str | None, bool] = (
        [],
        None,
        False,
    )
    fills_page: tuple[list[dict[str, Any]], str | None, bool] = (
        [],
        None,
        False,
    )
    alerts_page: tuple[list[dict[str, Any]], str | None, bool] = (
        [],
        None,
        False,
    )
    nav: Decimal | None = None
    realized_pnl_aggregates: tuple[Decimal, Decimal, Decimal, Decimal] = (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
    last_signals_filter: dict[str, Any] | None = None
    last_alerts_filter: dict[str, Any] | None = None
    last_orders_filter: dict[str, Any] | None = None
    trades_page: tuple[list[dict[str, Any]], str | None, bool] = (
        [],
        None,
        False,
    )
    trade_by_id: dict[str, Any] | None = None
    last_trades_filter: dict[str, Any] | None = None
    last_trade_by_id_arg: UUID | None = None
    parameter_set_row: ParameterSetRow | None = None
    audit_log_page: tuple[list[AuditLogRow], int | None, bool] = (
        [],
        None,
        False,
    )
    last_audit_filter: dict[str, Any] | None = None
    signal_summary: dict[str, Any] | None = None
    last_signal_summary_args: tuple[UUID, UUID] | None = None

    def __post_init__(self) -> None:
        if self.positions_rows is None:
            self.positions_rows = []
        if self.recon_row is None:
            self.recon_row = ReconciliationSummaryRow(
                last_check_utc=None,
                last_check_passed=True,
                open_breaks=0,
                breaks_24h=0,
            )
        if self.alert_counts is None:
            self.alert_counts = AlertCountRow(p0=0, p1=0, p2=0)

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account_id

    async def fetch_risk_state_current(self, account_id: UUID) -> RiskStateRow | None:
        return self.risk_row

    async def fetch_reconciliation_summary(self, account_id: UUID) -> ReconciliationSummaryRow:
        assert self.recon_row is not None  # appeased post_init
        return self.recon_row

    async def fetch_watchdog_last_ping_utc(self, account_id: UUID) -> datetime | None:
        return self.watchdog_ping

    async def count_pending_signals(self, account_id: UUID) -> int:
        return self.pending_signals

    async def count_open_alerts_by_severity(self, account_id: UUID) -> AlertCountRow:
        assert self.alert_counts is not None
        return self.alert_counts

    async def fetch_signals_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        self.last_signals_filter = {"status": status, "cursor": cursor, "limit": limit}
        return self.signals_page

    async def fetch_positions_current(
        self,
        account_id: UUID,
    ) -> list[dict[str, Any]]:
        return self.positions_rows

    async def fetch_orders_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        self.last_orders_filter = {"status": status, "cursor": cursor, "limit": limit}
        return self.orders_page

    async def fetch_fills_page(
        self,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        return self.fills_page

    async def fetch_alerts_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        severity: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        self.last_alerts_filter = {
            "status": status,
            "severity": severity,
            "cursor": cursor,
            "limit": limit,
        }
        return self.alerts_page

    async def fetch_trades_page(
        self,
        account_id: UUID,
        *,
        from_utc: datetime | None,
        to_utc: datetime | None,
        market: str | None,
        state: str | None,
        id_prefix: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        self.last_trades_filter = {
            "from_utc": from_utc,
            "to_utc": to_utc,
            "market": market,
            "state": state,
            "id_prefix": id_prefix,
            "cursor": cursor,
            "limit": limit,
        }
        return self.trades_page

    async def fetch_trade_by_id(
        self,
        account_id: UUID,
        trade_id: UUID,
    ) -> dict[str, Any] | None:
        self.last_trade_by_id_arg = trade_id
        return self.trade_by_id

    async def fetch_active_parameter_set(self) -> ParameterSetRow | None:
        return self.parameter_set_row

    async def fetch_audit_log_page(
        self,
        *,
        event_type: str | None,
        env: str | None,
        from_ingest_utc: datetime | None,
        to_ingest_utc: datetime | None,
        before_sequence_no: int | None,
        limit: int,
    ) -> tuple[list[AuditLogRow], int | None, bool]:
        self.last_audit_filter = {
            "event_type": event_type,
            "env": env,
            "from_ingest_utc": from_ingest_utc,
            "to_ingest_utc": to_ingest_utc,
            "before_sequence_no": before_sequence_no,
            "limit": limit,
        }
        return self.audit_log_page

    async def fetch_latest_balance_nav(self, account_id: UUID) -> Decimal | None:
        return self.nav

    async def fetch_realized_pnl_aggregates(
        self,
        account_id: UUID,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        return self.realized_pnl_aggregates

    async def fetch_signal_summary(
        self,
        account_id: UUID,
        signal_id: UUID,
    ) -> dict[str, object] | None:
        self.last_signal_summary_args = (account_id, signal_id)
        return self.signal_summary


def _bind_repo(override_dep: Any, route_module: Any, repo: Phase1QueryRepo) -> None:
    override_dep(route_module._get_repo, lambda: repo)


# Double-submit CSRF tokens used for state-changing endpoints. Cookie + header
# must MATCH (per services/api/middleware.CSRFMiddleware); the value is opaque.
_CSRF_TOKEN = "test-csrf-token-day15"


def _csrf_kwargs() -> dict[str, dict[str, str]]:
    """Return cookie + header kwargs for a CSRF-protected POST request.

    Mirrors the frontend's double-submit pattern: the same opaque token sits
    in the ``__Host-csrf_token`` cookie AND the ``X-CSRF-Token`` header. The
    middleware compares them for equality.
    """
    return {
        "cookies": {"__Host-csrf_token": _CSRF_TOKEN},
        "headers": {"X-CSRF-Token": _CSRF_TOKEN},
    }


# ---------------------------------------------------------------------------
# Auth / session
# ---------------------------------------------------------------------------


class TestAuthMe:
    @pytest.mark.asyncio
    async def test_returns_phase0_stub_session_envelope(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get("/api/auth/me")
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "phase0-stub-owner"
        assert body["username"] == "operator"
        assert body["role"] == "owner"
        # Day 25: Phase-0 stub realigned to "strong" so the 5-min re-auth
        # gate on risk-loosening endpoints is reachable in paper/dev.
        # See services/api/session._build_phase0_stub_session docstring.
        assert body["auth_strength"] == "strong"
        assert body["webauthn_enrolled"] is False
        assert body["totp_enrolled"] is False
        assert body["last_uv_at"] is not None
        assert body["session_expires_at"] is not None

    @pytest.mark.asyncio
    async def test_response_schema_has_no_extra_fields(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get("/api/auth/me")
        assert response.status_code == 200
        body = response.json()
        expected = {
            "user_id",
            "username",
            "role",
            "auth_strength",
            "last_uv_at",
            "session_expires_at",
            "webauthn_enrolled",
            "totp_enrolled",
        }
        assert set(body.keys()) == expected


class TestSessionStub:
    @pytest.mark.asyncio
    async def test_health_endpoint_is_session_exempt(
        self,
        api_client: AsyncClient,
    ) -> None:
        # /api/health was Day 5; should still pass through without a session.
        response = await api_client.get("/api/health")
        # Day 5 health route returns 503 if pool isn't initialized; the
        # important thing is that the session middleware did not 401.
        assert response.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_protected_endpoint_gets_phase0_session(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Hits /api/auth/me without any cookie; should get the stub session.
        response = await api_client.get("/api/auth/me")
        assert response.status_code == 200


class TestSessionStubCsrfBootstrap:
    """Regression tests for the 2026-05-28 CSRF-resume bug.

    Pre-fix repro: operator visits ``/system`` in a fresh browser without
    going through ``/login`` first; the SessionStub injects a strong
    session but never mints a CSRF cookie; the first state-changing POST
    (Resume kill switch) is rejected by :class:`CSRFMiddleware` with
    ``CSRF_REJECTED``. See ``Docs/decisions-log.md`` 2026-05-28 entry.

    Post-fix invariant: every Phase-0 stub-injected response that arrived
    without a CSRF cookie carries a server-minted one in the response's
    ``Set-Cookie`` header. The next request can then send the cookie value
    back as both cookie + ``X-CSRF-Token`` and pass the double-submit gate.
    """

    @pytest.mark.asyncio
    async def test_response_sets_csrf_cookie_when_request_lacked_one(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Fresh client → no cookies in the request. The stub should attach a
        # Set-Cookie for __Host-csrf_token on the response.
        response = await api_client.get("/api/auth/me")
        assert response.status_code == 200
        # httpx normalises Set-Cookie into response.cookies (the cookie jar);
        # the raw header is also available via response.headers.get_list.
        set_cookie_headers = response.headers.get_list("set-cookie")
        csrf_set_cookies = [h for h in set_cookie_headers if "__Host-csrf_token=" in h]
        assert len(csrf_set_cookies) == 1, (
            f"Expected exactly one Set-Cookie for __Host-csrf_token, got: {set_cookie_headers!r}"
        )
        raw = csrf_set_cookies[0]
        # Required attributes for the __Host- prefix (browser-side invariants).
        # Note: in this test the env is "dev" so Secure is absent; in paper/live
        # the Secure flag is set (covered by test below).
        assert "Path=/" in raw or "path=/" in raw
        assert "SameSite=strict" in raw or "samesite=strict" in raw.lower()
        # Max-Age matches session_absolute_seconds (24h default) so the cookie
        # survives browser restarts the same way a server-issued cookie would.
        assert "Max-Age=86400" in raw or "max-age=86400" in raw.lower()

    @pytest.mark.asyncio
    async def test_response_does_not_set_csrf_cookie_when_request_had_one(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Pre-existing cookie → the stub should NOT churn the value by minting
        # a new one. Idempotency matters: a long-lived browser session would
        # otherwise get its CSRF cookie reset on every page load, which is
        # both wasteful and would invalidate any in-flight double-submit
        # token currently held by the frontend.
        response = await api_client.get(
            "/api/auth/me",
            cookies={"__Host-csrf_token": "preexisting-value-from-prior-login"},
        )
        assert response.status_code == 200
        set_cookie_headers = response.headers.get_list("set-cookie")
        csrf_set_cookies = [h for h in set_cookie_headers if "__Host-csrf_token=" in h]
        assert csrf_set_cookies == [], (
            f"Stub minted a CSRF cookie when one was already present: {set_cookie_headers!r}"
        )

    @pytest.mark.asyncio
    async def test_bootstrapped_csrf_cookie_unlocks_state_changing_post(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        # End-to-end repro of the operator's broken flow:
        #   1. Fresh browser → GET /api/auth/me (mounts /system page).
        #   2. Server bootstraps CSRF cookie via Set-Cookie.
        #   3. Frontend reads document.cookie + sends value as X-CSRF-Token.
        #   4. POST to a CSRF-gated endpoint passes the double-submit check.
        # Without the fix, step 2 wouldn't happen and step 4 would 403 with
        # CSRF_REJECTED. We use /api/signals/<id>/approve as the canary —
        # same Depends(_get_repo) pattern as kill-switch/resume but with no
        # `Depends(get_session)` DB dependency (the unit-test conftest skips
        # the lifespan, so the real DB pool isn't initialised). The route
        # handler returns 409 NO_ACTIVE_ACCOUNT cleanly with a stub repo —
        # any response other than 403 CSRF_REJECTED proves the gate passed.
        bootstrap_response = await api_client.get("/api/auth/me")
        assert bootstrap_response.status_code == 200
        csrf_cookie_value = bootstrap_response.cookies.get("__Host-csrf_token")
        assert csrf_cookie_value is not None, (
            "Stub did not bootstrap a CSRF cookie on the GET response"
        )
        # Wire a no-account stub repo so the handler bails at 409 instead of
        # touching DB-backed code paths.
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, signals_route, repo)
        follow_up = await api_client.post(
            f"/api/signals/{uuid4()}/approve",
            json={"override_size": 1},
            cookies={"__Host-csrf_token": csrf_cookie_value},
            headers={"X-CSRF-Token": csrf_cookie_value},
        )
        # Specific assertion: the request passed CSRF and reached the route
        # handler. The handler then returns 409 NO_ACTIVE_ACCOUNT per the
        # stub repo's account_id=None.
        assert follow_up.status_code == 409, (
            f"Expected 409 (NO_ACTIVE_ACCOUNT) after CSRF passed; "
            f"got status={follow_up.status_code} body={follow_up.text!r}"
        )
        body = follow_up.json()
        assert body["error_code"] == "NO_ACTIVE_ACCOUNT", (
            f"Expected NO_ACTIVE_ACCOUNT after CSRF passed; got {body!r}"
        )


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class TestListSignals:
    @pytest.mark.asyncio
    async def test_no_active_account_returns_empty_list(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, signals_route, repo)
        response = await api_client.get("/api/signals")
        assert response.status_code == 200
        body = response.json()
        assert body == {"items": [], "next_cursor": None, "has_more": False}

    @pytest.mark.asyncio
    async def test_status_filter_passes_through_to_repo(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=uuid4())
        _bind_repo(override_dep, signals_route, repo)
        response = await api_client.get("/api/signals?status=pending&limit=25")
        assert response.status_code == 200
        assert repo.last_signals_filter == {
            "status": "pending",
            "cursor": None,
            "limit": 25,
        }


class TestSignalApprove:
    """Pivot-PR-D (post-pivot 2026-05-12): the 501 stub is replaced by a real
    dispatcher call. Tests now verify the no-account branch returns 409
    NO_ACTIVE_ACCOUNT — exercising the dispatcher's full path requires a real
    DB (covered separately by ``test_risk_signal_dispatch.py``).
    """

    @pytest.mark.asyncio
    async def test_no_account_returns_409(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, signals_route, repo)
        signal_id = uuid4()
        response = await api_client.post(
            f"/api/signals/{signal_id}/approve",
            json={"override_size": 3},
            **_csrf_kwargs(),
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "NO_ACTIVE_ACCOUNT"

    @pytest.mark.asyncio
    async def test_unknown_field_rejected_with_validation_error(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, signals_route, repo)
        signal_id = uuid4()
        response = await api_client.post(
            f"/api/signals/{signal_id}/approve",
            json={"override_size": 3, "extra": "nope"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error_code"] == "VALIDATION_ERROR"


class TestSignalReject:
    @pytest.mark.asyncio
    async def test_with_diary_entry_no_account_returns_409(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, signals_route, repo)
        signal_id = uuid4()
        response = await api_client.post(
            f"/api/signals/{signal_id}/reject",
            json={
                "decision_diary_entry": {
                    "tag": "data_concern",
                    "reasoning_text": "vol regime is elevated above z=2 threshold",
                },
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "NO_ACTIVE_ACCOUNT"

    @pytest.mark.asyncio
    async def test_missing_diary_entry_returns_422(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, signals_route, repo)
        signal_id = uuid4()
        response = await api_client.post(
            f"/api/signals/{signal_id}/reject",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 422


class TestSignalDefer:
    @pytest.mark.asyncio
    async def test_with_diary_entry_no_account_returns_409(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, signals_route, repo)
        signal_id = uuid4()
        response = await api_client.post(
            f"/api/signals/{signal_id}/defer",
            json={
                "decision_diary_entry": {
                    "tag": "manual_judgment",
                    "reasoning_text": "want to wait for FOMC tomorrow before adding risk",
                },
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "NO_ACTIVE_ACCOUNT"


class TestSignalDecisionSSEEmit:
    """Verify approve/reject/defer fire a `signal` SSE event on success.

    The dispatcher is monkeypatched (it requires a real Postgres session
    to walk the audit-first flow); we exercise only the route handler's
    SSE emission path with a captured fake.
    """

    @pytest.mark.asyncio
    async def test_approve_emits_signal_sse_on_success(
        self,
        api_client: AsyncClient,
        override_dep: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = uuid4()
        signal_id = uuid4()
        audit_uuid = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            signal_summary={
                "market": "/MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": Decimal("5250.00"),
                "strategy_hash": "0" * 40,
                "parameter_set_hash": "0" * 64,
                "emitted_at_utc": datetime.now(tz=UTC),
                "env": "paper",
                "status": "pending",
            },
        )
        _bind_repo(override_dep, signals_route, repo)

        captured_emits: list[tuple[str, dict[str, Any]]] = []

        async def fake_emit_sse(event_type: str, data: dict[str, Any]) -> int:
            captured_emits.append((event_type, data))
            return 99

        async def fake_apply(plan: Any, **_: Any) -> Any:
            from services.risk.signal_dispatch import SignalDispatchResult

            return SignalDispatchResult(
                signal_id=plan.signal_id,
                action=plan.action,
                new_status=plan.new_status,
                audit_event_uuid=audit_uuid,
                audit_sequence_no=42,
            )

        monkeypatch.setattr(signals_route, "emit_sse", fake_emit_sse)
        from services.api import db as api_db_module
        from services.risk import signal_dispatch as sd_module

        monkeypatch.setattr(sd_module, "apply_signal_dispatch", fake_apply)
        monkeypatch.setattr(api_db_module, "get_session_factory", lambda: object())
        # PR-H: approve_signal reads risk_state before dispatch. Stub
        # the helper to return NORMAL so the gate passes; reject/defer
        # tests below don't exercise this path (the route only calls
        # fetch_current_risk_state on approve), but patching is a
        # no-op for those + keeps the fixture symmetric.
        monkeypatch.setattr(sd_module, "fetch_current_risk_state", AsyncMock(return_value="NORMAL"))

        response = await api_client.post(
            f"/api/signals/{signal_id}/approve",
            json={"override_size": 1},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        assert len(captured_emits) == 1
        event_type, data = captured_emits[0]
        assert event_type == "signal"
        assert data["action"] == "approved"
        assert data["signal_id"] == str(signal_id)
        assert data["market"] == "/MES"
        assert data["direction"] == "long"
        assert data["decided_by_user_id"] == "phase0-stub-owner"
        assert data["audit_event_uuid"] == str(audit_uuid)
        assert "diary_reasoning_text" not in data
        assert repo.last_signal_summary_args == (account_id, signal_id)

    @pytest.mark.asyncio
    async def test_reject_emits_signal_sse_with_diary_reasoning(
        self,
        api_client: AsyncClient,
        override_dep: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = uuid4()
        signal_id = uuid4()
        audit_uuid = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            signal_summary={
                "market": "/MNQ",
                "direction": "short",
                "target_contracts": 1,
                "decision_price": Decimal("18250.00"),
                "strategy_hash": "0" * 40,
                "parameter_set_hash": "0" * 64,
                "emitted_at_utc": datetime.now(tz=UTC),
                "env": "paper",
                "status": "pending",
            },
        )
        _bind_repo(override_dep, signals_route, repo)

        captured_emits: list[tuple[str, dict[str, Any]]] = []

        async def fake_emit_sse(event_type: str, data: dict[str, Any]) -> int:
            captured_emits.append((event_type, data))
            return 100

        async def fake_apply(plan: Any, **_: Any) -> Any:
            from services.risk.signal_dispatch import SignalDispatchResult

            return SignalDispatchResult(
                signal_id=plan.signal_id,
                action=plan.action,
                new_status=plan.new_status,
                audit_event_uuid=audit_uuid,
                audit_sequence_no=43,
            )

        monkeypatch.setattr(signals_route, "emit_sse", fake_emit_sse)
        from services.api import db as api_db_module
        from services.risk import signal_dispatch as sd_module

        monkeypatch.setattr(sd_module, "apply_signal_dispatch", fake_apply)
        monkeypatch.setattr(api_db_module, "get_session_factory", lambda: object())
        # PR-H: approve_signal reads risk_state before dispatch. Stub
        # the helper to return NORMAL so the gate passes; reject/defer
        # tests below don't exercise this path (the route only calls
        # fetch_current_risk_state on approve), but patching is a
        # no-op for those + keeps the fixture symmetric.
        monkeypatch.setattr(sd_module, "fetch_current_risk_state", AsyncMock(return_value="NORMAL"))

        response = await api_client.post(
            f"/api/signals/{signal_id}/reject",
            json={
                "decision_diary_entry": {
                    "tag": "regime_concern",
                    "reasoning_text": "vol regime elevated above z=2; defer entry",
                },
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        assert len(captured_emits) == 1
        _, data = captured_emits[0]
        assert data["action"] == "rejected"
        assert data["market"] == "/MNQ"
        assert data["direction"] == "short"
        assert data["diary_reasoning_text"] == "vol regime elevated above z=2; defer entry"

    @pytest.mark.asyncio
    async def test_defer_emits_signal_sse_with_action_deferred(
        self,
        api_client: AsyncClient,
        override_dep: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = uuid4()
        signal_id = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            signal_summary={
                "market": "TLT",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": Decimal("92.50"),
                "strategy_hash": "0" * 40,
                "parameter_set_hash": "0" * 64,
                "emitted_at_utc": datetime.now(tz=UTC),
                "env": "paper",
                "status": "pending",
            },
        )
        _bind_repo(override_dep, signals_route, repo)
        captured_emits: list[tuple[str, dict[str, Any]]] = []

        async def fake_emit_sse(event_type: str, data: dict[str, Any]) -> int:
            captured_emits.append((event_type, data))
            return 101

        async def fake_apply(plan: Any, **_: Any) -> Any:
            from services.risk.signal_dispatch import SignalDispatchResult

            return SignalDispatchResult(
                signal_id=plan.signal_id,
                action=plan.action,
                new_status=plan.new_status,
                audit_event_uuid=uuid4(),
                audit_sequence_no=44,
            )

        monkeypatch.setattr(signals_route, "emit_sse", fake_emit_sse)
        from services.api import db as api_db_module
        from services.risk import signal_dispatch as sd_module

        monkeypatch.setattr(sd_module, "apply_signal_dispatch", fake_apply)
        monkeypatch.setattr(api_db_module, "get_session_factory", lambda: object())
        # PR-H: approve_signal reads risk_state before dispatch. Stub
        # the helper to return NORMAL so the gate passes; reject/defer
        # tests below don't exercise this path (the route only calls
        # fetch_current_risk_state on approve), but patching is a
        # no-op for those + keeps the fixture symmetric.
        monkeypatch.setattr(sd_module, "fetch_current_risk_state", AsyncMock(return_value="NORMAL"))

        response = await api_client.post(
            f"/api/signals/{signal_id}/defer",
            json={
                "decision_diary_entry": {
                    "tag": "manual_judgment",
                    "reasoning_text": "waiting for FOMC tomorrow",
                },
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        assert len(captured_emits) == 1
        _, data = captured_emits[0]
        assert data["action"] == "deferred"
        assert data["market"] == "TLT"

    @pytest.mark.asyncio
    async def test_approve_emit_failure_does_not_fail_request(
        self,
        api_client: AsyncClient,
        override_dep: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If emit_sse raises, the request still returns 200; audit + signal row are durable."""
        account_id = uuid4()
        signal_id = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            signal_summary={
                "market": "/MES",
                "direction": "long",
                "target_contracts": 1,
                "decision_price": Decimal("5250.00"),
                "strategy_hash": "0" * 40,
                "parameter_set_hash": "0" * 64,
                "emitted_at_utc": datetime.now(tz=UTC),
                "env": "paper",
                "status": "pending",
            },
        )
        _bind_repo(override_dep, signals_route, repo)

        async def boom(_event_type: str, _data: dict[str, Any]) -> int:
            raise RuntimeError("SSE multiplexer crashed")

        async def fake_apply(plan: Any, **_: Any) -> Any:
            from services.risk.signal_dispatch import SignalDispatchResult

            return SignalDispatchResult(
                signal_id=plan.signal_id,
                action=plan.action,
                new_status=plan.new_status,
                audit_event_uuid=uuid4(),
                audit_sequence_no=45,
            )

        monkeypatch.setattr(signals_route, "emit_sse", boom)
        from services.api import db as api_db_module
        from services.risk import signal_dispatch as sd_module

        monkeypatch.setattr(sd_module, "apply_signal_dispatch", fake_apply)
        monkeypatch.setattr(api_db_module, "get_session_factory", lambda: object())
        # PR-H: approve_signal reads risk_state before dispatch. Stub
        # the helper to return NORMAL so the gate passes; reject/defer
        # tests below don't exercise this path (the route only calls
        # fetch_current_risk_state on approve), but patching is a
        # no-op for those + keeps the fixture symmetric.
        monkeypatch.setattr(sd_module, "fetch_current_risk_state", AsyncMock(return_value="NORMAL"))

        response = await api_client.post(
            f"/api/signals/{signal_id}/approve",
            json={"override_size": 1},
            **_csrf_kwargs(),
        )
        # Audit + signal-row write succeeded; SSE failure is logged but
        # doesn't propagate.
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_approve_no_summary_skips_emit_without_crash(
        self,
        api_client: AsyncClient,
        override_dep: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If fetch_signal_summary returns None, the SSE emit is skipped."""
        account_id = uuid4()
        signal_id = uuid4()
        repo = _StubRepo(account_id=account_id, signal_summary=None)
        _bind_repo(override_dep, signals_route, repo)
        captured_emits: list[tuple[str, dict[str, Any]]] = []

        async def fake_emit_sse(event_type: str, data: dict[str, Any]) -> int:
            captured_emits.append((event_type, data))
            return 99

        async def fake_apply(plan: Any, **_: Any) -> Any:
            from services.risk.signal_dispatch import SignalDispatchResult

            return SignalDispatchResult(
                signal_id=plan.signal_id,
                action=plan.action,
                new_status=plan.new_status,
                audit_event_uuid=uuid4(),
                audit_sequence_no=46,
            )

        monkeypatch.setattr(signals_route, "emit_sse", fake_emit_sse)
        from services.api import db as api_db_module
        from services.risk import signal_dispatch as sd_module

        monkeypatch.setattr(sd_module, "apply_signal_dispatch", fake_apply)
        monkeypatch.setattr(api_db_module, "get_session_factory", lambda: object())
        # PR-H: approve_signal reads risk_state before dispatch. Stub
        # the helper to return NORMAL so the gate passes; reject/defer
        # tests below don't exercise this path (the route only calls
        # fetch_current_risk_state on approve), but patching is a
        # no-op for those + keeps the fixture symmetric.
        monkeypatch.setattr(sd_module, "fetch_current_risk_state", AsyncMock(return_value="NORMAL"))

        response = await api_client.post(
            f"/api/signals/{signal_id}/approve",
            json={"override_size": 1},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200
        # No emit because summary was None.
        assert captured_emits == []


# ---------------------------------------------------------------------------
# System / kill-switch
# ---------------------------------------------------------------------------


class TestSystemStatus:
    @pytest.mark.asyncio
    async def test_no_account_returns_default_normal_state(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/status")
        assert response.status_code == 200
        body = response.json()
        assert body["risk_state"] == "NORMAL"
        assert body["severity"] is None
        assert body["halt_reason"] is None
        assert body["halt_dwell_session_count"] is None
        assert body["convalescent_session_count"] is None
        assert body["vacation_active"] is False
        assert body["reconciliation_summary"]["open_breaks"] == 0
        assert body["reconciliation_summary"]["last_check_passed"] is True
        # Epoch sentinel for never-run watchdog
        assert body["watchdog_last_ping_utc"].startswith("1970-01-01")
        assert body["server_now"] is not None
        assert body["backend_version"] == "test"

    @pytest.mark.asyncio
    async def test_halt_new_state_populates_halt_reason(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        account_id = uuid4()
        now = datetime.now(tz=UTC)
        repo = _StubRepo(
            account_id=account_id,
            risk_row=RiskStateRow(
                state="HALT_NEW",
                severity="routine",
                reason="trailing_dd_breach",
                entered_at_utc=now,
                convalescent_session_count=0,
                vacation_active=False,
                vacation_until_utc=None,
                audit_event_uuid=uuid4(),
            ),
        )
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/status")
        assert response.status_code == 200
        body = response.json()
        assert body["risk_state"] == "HALT_NEW"
        assert body["severity"] == "routine"
        assert body["halt_reason"] == "trailing_dd_breach"
        assert body["halt_dwell_session_count"] == 0
        assert body["convalescent_session_count"] is None


class TestKillSwitchStatus:
    @pytest.mark.asyncio
    async def test_no_account_returns_default_normal(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/kill-switch")
        assert response.status_code == 200
        body = response.json()
        assert body["risk_state"] == "NORMAL"
        assert body["severity"] is None
        assert body["halt_reason"] is None
        assert body["last_transition_audit_event_uuid"] is None


class TestKillSwitchInvoke:
    """Day 25 (Week 7 Mon): the 501 stubs become real handlers backed by
    ``services.risk.state_machine`` + ``services.risk.dispatch``.

    Unit tests monkeypatch ``apply_state_transition`` + ``emit_sse`` at the
    route-module level so the route's plan-then-apply orchestration is
    exercised without a real audit_log write. The end-to-end behavior
    (real Postgres + real audit chain + real SSE multiplexer) is covered
    in ``tests/integration/test_kill_switch_end_to_end.py``.
    """

    @pytest.mark.asyncio
    async def test_no_active_account_returns_409(
        self,
        api_client: AsyncClient,
        api_app: Any,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, system_route, repo)
        _override_db_session(api_app)
        response = await api_client.post(
            "/api/system/kill-switch/invoke",
            json={"trigger": "manual_judgment", "reason": "operator paused"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "NO_ACTIVE_ACCOUNT"

    @pytest.mark.asyncio
    async def test_normal_to_halt_returns_200_with_envelope(
        self,
        api_client: AsyncClient,
        api_app: Any,
        override_dep: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = uuid4()
        repo = _StubRepo(account_id=account_id, risk_row=None)  # no row → NORMAL default
        _bind_repo(override_dep, system_route, repo)
        # Bypass the DB session dep (apply_state_transition gets the
        # stubbed fake session; we monkeypatch apply itself so the session
        # is never used).
        _override_db_session(api_app)

        applied_audit_uuid = uuid4()
        fake_apply = _make_fake_apply(
            applied_audit_uuid,
            new_state="HALT_NEW",
            new_severity="routine",
        )
        monkeypatch.setattr(system_route, "apply_state_transition", fake_apply)
        monkeypatch.setattr(
            system_route,
            "emit_sse",
            _make_fake_emit_sse(captured_sequence_no=42),
        )

        response = await api_client.post(
            "/api/system/kill-switch/invoke",
            json={"trigger": "manual_judgment", "reason": "operator paused"},
            **_csrf_kwargs(),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["risk_state"] == "HALT_NEW"
        assert body["severity"] == "routine"
        assert body["halt_reason"] == "manual_judgment"
        assert body["audit_event_uuid"] == str(applied_audit_uuid)
        assert body["sse_sequence_no"] == 42

    @pytest.mark.asyncio
    async def test_already_halted_returns_409(
        self,
        api_client: AsyncClient,
        api_app: Any,
        override_dep: Any,
    ) -> None:
        account_id = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            risk_row=RiskStateRow(
                state="HALT_NEW",
                severity="routine",
                reason="trailing_dd_breach",
                entered_at_utc=datetime.now(tz=UTC),
                convalescent_session_count=0,
                vacation_active=False,
                vacation_until_utc=None,
                audit_event_uuid=uuid4(),
            ),
        )
        _bind_repo(override_dep, system_route, repo)
        _override_db_session(api_app)
        response = await api_client.post(
            "/api/system/kill-switch/invoke",
            json={"trigger": "manual_judgment", "reason": "redundant"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "ALREADY_HALTED"
        # The error message echoes the existing severity + reason so the
        # operator can decide whether to /resume or accept the no-op.
        assert "routine" in body["message"]
        assert "trailing_dd_breach" in body["message"]

    @pytest.mark.asyncio
    async def test_invalid_trigger_returns_422(
        self,
        api_client: AsyncClient,
        api_app: Any,
    ) -> None:
        # Body validation runs before DB deps resolve, so no override needed,
        # but defensively override anyway so the test isn't sensitive to
        # FastAPI's dep-resolution ordering changes upstream.
        _override_db_session(api_app)
        response = await api_client.post(
            "/api/system/kill-switch/invoke",
            json={"trigger": "not_a_real_trigger", "reason": "x"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 422


class TestKillSwitchResume:
    """Day 25 (Week 7 Mon): real resume handler with 5-min UV re-auth gate."""

    @pytest.mark.asyncio
    async def test_no_active_account_returns_409(
        self,
        api_client: AsyncClient,
        api_app: Any,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, system_route, repo)
        _override_db_session(api_app)
        response = await api_client.post(
            "/api/system/kill-switch/resume",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "NO_ACTIVE_ACCOUNT"

    @pytest.mark.asyncio
    async def test_not_halted_returns_409(
        self,
        api_client: AsyncClient,
        api_app: Any,
        override_dep: Any,
    ) -> None:
        account_id = uuid4()
        repo = _StubRepo(account_id=account_id, risk_row=None)  # → NORMAL default
        _bind_repo(override_dep, system_route, repo)
        _override_db_session(api_app)
        response = await api_client.post(
            "/api/system/kill-switch/resume",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "NOT_HALTED"
        assert "NORMAL" in body["message"]

    @pytest.mark.asyncio
    async def test_routine_halt_to_convalescent_returns_200(
        self,
        api_client: AsyncClient,
        api_app: Any,
        override_dep: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            risk_row=RiskStateRow(
                state="HALT_NEW",
                severity="routine",
                reason="trailing_dd_breach",
                entered_at_utc=datetime.now(tz=UTC),
                convalescent_session_count=0,
                vacation_active=False,
                vacation_until_utc=None,
                audit_event_uuid=uuid4(),
            ),
        )
        _bind_repo(override_dep, system_route, repo)
        _override_db_session(api_app)

        applied_audit_uuid = uuid4()
        monkeypatch.setattr(
            system_route,
            "apply_state_transition",
            _make_fake_apply(
                applied_audit_uuid,
                new_state="CONVALESCENT",
                new_severity=None,
            ),
        )
        monkeypatch.setattr(
            system_route,
            "emit_sse",
            _make_fake_emit_sse(captured_sequence_no=7),
        )

        response = await api_client.post(
            "/api/system/kill-switch/resume",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["risk_state"] == "CONVALESCENT"
        assert body["severity"] is None
        assert body["halt_reason"] is None
        assert body["audit_event_uuid"] == str(applied_audit_uuid)
        assert body["sse_sequence_no"] == 7

    @pytest.mark.asyncio
    async def test_incident_review_without_id_returns_422(
        self,
        api_client: AsyncClient,
        api_app: Any,
        override_dep: Any,
    ) -> None:
        account_id = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            risk_row=RiskStateRow(
                state="HALT_NEW",
                severity="incident_review",
                reason="hash_chain_break",
                entered_at_utc=datetime.now(tz=UTC),
                convalescent_session_count=0,
                vacation_active=False,
                vacation_until_utc=None,
                audit_event_uuid=uuid4(),
            ),
        )
        _bind_repo(override_dep, system_route, repo)
        _override_db_session(api_app)
        response = await api_client.post(
            "/api/system/kill-switch/resume",
            json={},  # missing incident_review_id
            **_csrf_kwargs(),
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error_code"] == "INCIDENT_REVIEW_ID_REQUIRED"

    @pytest.mark.asyncio
    async def test_re_auth_required_when_session_weak(
        self,
        api_client: AsyncClient,
        api_app: Any,
    ) -> None:
        # Override get_session_context to inject a weak session — re-auth
        # gate fails first; no DB lookup happens. Proves the resume route
        # enforces the dev-guide §1.5 5-min UV gate.
        from datetime import timedelta as _td

        from services.api.session import SessionContext, get_session_context

        weak_session = SessionContext(
            user_id="phase0-stub-owner",
            username="operator",
            role="owner",
            auth_strength="weak",
            last_uv_at=datetime.now(tz=UTC) - _td(minutes=1),
            session_expires_at=datetime.now(tz=UTC) + _td(minutes=30),
            webauthn_enrolled=False,
            totp_enrolled=False,
            is_phase0_stub=True,
        )
        api_app.dependency_overrides[get_session_context] = lambda: weak_session
        _override_db_session(api_app)

        response = await api_client.post(
            "/api/system/kill-switch/resume",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error_code"] == "RE_AUTH_REQUIRED"


# ---------------------------------------------------------------------------
# Test-only helpers — fake DB session + apply + emit_sse for Day 25 routes.
# ---------------------------------------------------------------------------


class _NoopDbSession:
    """Placeholder session that errors loudly if anything tries to use it.

    The route's ``Depends(get_session)`` is overridden via this so the
    asyncpg pool stays out of unit tests; ``apply_state_transition`` is
    also monkeypatched in the same test, so the session is never actually
    touched. Calls into it surface a clear error.

    ``commit()`` is a real no-op because Day-25 DP-021 fix added
    ``await db.commit()`` between the repo reads and apply_state_transition
    to release the implicit transaction the repo opened. Unit tests don't
    issue real SELECTs against this fake (the repo is stubbed), but the
    route still calls commit() — make it a no-op so the route reaches its
    happy path.
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise RuntimeError(
            "fake _NoopDbSession.execute called — monkeypatch apply_state_transition first"
        )

    async def commit(self) -> None:
        # DP-021 fix calls this between repo reads and apply. No-op for
        # the fake (no real transaction to commit; the real DB-touching
        # apply_state_transition is monkeypatched in these tests).
        return None


async def _fake_db_session_dependency() -> Any:
    """Async generator dep replacement matching ``get_session``'s shape."""
    yield _NoopDbSession()


def _override_db_session(api_app: Any) -> None:
    """Wire ``_fake_db_session_dependency`` over ``get_session``.

    FastAPI resolves all deps for a route up-front, so even the early
    409/422 error branches need a valid db session dep — otherwise
    ``init_pool() first`` raises before the route handler sees the
    short-circuit branch.
    """
    from services.api.db import get_session

    api_app.dependency_overrides[get_session] = _fake_db_session_dependency


def _make_fake_apply(
    applied_audit_uuid: Any,
    *,
    new_state: str,
    new_severity: str | None,
) -> Any:
    """Build an async stub for ``apply_state_transition`` returning a canned
    :class:`AppliedStateTransition`.
    """
    from services.risk.dispatch import AppliedStateTransition

    async def _apply(**kwargs: Any) -> AppliedStateTransition:
        return AppliedStateTransition(
            state_transition_audit_event_uuid=applied_audit_uuid,
            new_state=new_state,
            new_severity=new_severity,
        )

    return _apply


def _make_fake_emit_sse(*, captured_sequence_no: int) -> Any:
    """Build an async stub for ``emit_sse`` returning a canned sequence_no."""

    async def _emit(event_type: str, data: dict[str, Any]) -> int:
        return captured_sequence_no

    return _emit


# ---------------------------------------------------------------------------
# Today / health-score
# ---------------------------------------------------------------------------


class TestHealthScore:
    @pytest.mark.asyncio
    async def test_returns_phase0_insufficient_data_envelope(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get("/api/health-score")
        assert response.status_code == 200
        body = response.json()
        assert body["insufficient_data"] is True
        assert body["traffic_light"] == "yellow"
        assert body["composite"] == 0
        assert len(body["components"]) == 5
        # Five locked component names
        names = [c["name"] for c in body["components"]]
        assert names == [
            "live_sharpe_vs_backtest",
            "slippage_drift",
            "hit_rate",
            "capacity_headroom",
            "days_since_recon_break",
        ]
        # Weights sum to 100
        total_weight = sum(c["weight_pct"] for c in body["components"])
        assert total_weight == 100
        # All scores null with insufficient_data=True
        for component in body["components"]:
            assert component["score"] is None
            assert component["insufficient_data"] is True


class TestTodayDigest:
    @pytest.mark.asyncio
    async def test_no_account_returns_zero_envelope(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, today_route, repo)
        response = await api_client.get("/api/today/digest")
        assert response.status_code == 200
        body = response.json()
        assert body["health_score"]["insufficient_data"] is True
        assert body["pnl"]["daily_pnl"] == "0"
        assert body["queued_signals_count"] == 0
        assert body["active_alerts_count_by_severity"] == {"P0": 0, "P1": 0, "P2": 0}
        assert body["state"] == "NORMAL"
        assert body["state_severity"] is None
        assert body["agent_status"] == "disabled"
        assert body["environment"] == "dev"

    @pytest.mark.asyncio
    async def test_vacation_active_overrides_state_to_vacation(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        account_id = uuid4()
        now = datetime.now(tz=UTC)
        repo = _StubRepo(
            account_id=account_id,
            risk_row=RiskStateRow(
                state="NORMAL",
                severity=None,
                reason=None,
                entered_at_utc=now,
                convalescent_session_count=0,
                vacation_active=True,
                vacation_until_utc=now + timedelta(days=3),
                audit_event_uuid=uuid4(),
            ),
            pending_signals=2,
            alert_counts=AlertCountRow(p0=0, p1=1, p2=3),
        )
        _bind_repo(override_dep, today_route, repo)
        response = await api_client.get("/api/today/digest")
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "VACATION"
        assert body["queued_signals_count"] == 2
        assert body["active_alerts_count_by_severity"] == {"P0": 0, "P1": 1, "P2": 3}


# ---------------------------------------------------------------------------
# Positions / orders / fills / alerts
# ---------------------------------------------------------------------------


class TestPositionsCurrent:
    @pytest.mark.asyncio
    async def test_no_account_returns_empty_envelope_with_as_of(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, positions_route, repo)
        response = await api_client.get("/api/positions/current")
        assert response.status_code == 200
        body = response.json()
        assert body["positions"] == []
        assert body["as_of"] is not None

    @pytest.mark.asyncio
    async def test_populated_positions_mapped_to_position_objects(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        """Post-2026-05-27 fix: route maps positions_current rows into
        Position objects with Phase 1 field defaults (cluster=None,
        contract_month=None). current_price + unrealized_pnl are
        computed on-read via the MTM helper (post-Task-3); when the
        LEAN data zip is not present in the default settings.bar_sync_data_root
        the helper falls back to (avg_cost, unrealized_pnl=0). The
        positions_current.unrealized_pnl column is IGNORED (no MTM
        writer populates it today — see services.api.position_mtm
        docstring)."""
        account_id = uuid4()
        contract_id = uuid4()
        positions_rows = [
            {
                "id": uuid4(),
                "market": "/M2K",
                "contract_id": contract_id,  # futures
                "quantity": 1,
                "avg_cost": Decimal("2925.00"),
                "margin_held": Decimal("500.00"),
                "unrealized_pnl": None,
                "last_mark_ts": datetime(2026, 5, 27, 13, 31, tzinfo=UTC),
                "managed_by_version": "b5237aed3f06288656a31510795a3ae3e76556ae",
            },
            {
                "id": uuid4(),
                "market": "TLT",
                "contract_id": None,  # ETF
                "quantity": 5,
                "avg_cost": Decimal("83.72"),
                "margin_held": Decimal("0"),
                # Stale legacy column; route ignores it post-Task-3.
                "unrealized_pnl": Decimal("12.50"),
                "last_mark_ts": datetime(2026, 5, 27, 13, 31, tzinfo=UTC),
                "managed_by_version": "c951158bdeadbeef" + "0" * 24,
            },
        ]
        repo = _StubRepo(
            account_id=account_id,
            positions_rows=positions_rows,
            nav=Decimal("100000"),
        )
        _bind_repo(override_dep, positions_route, repo)
        response = await api_client.get("/api/positions/current")
        assert response.status_code == 200
        body = response.json()
        assert len(body["positions"]) == 2

        m2k = next(p for p in body["positions"] if p["symbol"] == "/M2K")
        # Futures: instrument_id is the contract_id as a string
        assert m2k["instrument_id"] == str(contract_id)
        assert m2k["qty"] == 1
        assert Decimal(m2k["avg_entry_price"]) == Decimal("2925.00")
        # MTM helper falls back to avg_cost + unrealized_pnl=0 because the
        # default data_root in the test env has no fixture zips.
        assert Decimal(m2k["current_price"]) == Decimal("2925.00")
        assert Decimal(m2k["unrealized_pnl"]) == Decimal("0")
        assert Decimal(m2k["unrealized_pnl_pct_of_nav"]) == Decimal("0")
        # Phase 1 fallback: contracts JOIN deferred
        assert m2k["cluster"] is None
        assert m2k["contract_month"] is None
        # First 7 chars of managed_by_version per spec §4.1.5b
        assert m2k["managed_by_strategy_version"] == "b5237ae"

        tlt = next(p for p in body["positions"] if p["symbol"] == "TLT")
        # ETF: instrument_id falls back to market symbol when contract_id IS NULL
        assert tlt["instrument_id"] == "TLT"
        # No MTM data on disk → fallback. positions_current.unrealized_pnl
        # is intentionally ignored.
        assert Decimal(tlt["current_price"]) == Decimal("83.72")
        assert Decimal(tlt["unrealized_pnl"]) == Decimal("0")
        assert Decimal(tlt["unrealized_pnl_pct_of_nav"]) == Decimal("0")

    @pytest.mark.asyncio
    async def test_mtm_populated_when_lean_data_present(
        self,
        api_client: AsyncClient,
        override_dep: Any,
        tmp_path: Any,
    ) -> None:
        """When the LEAN on-disk zip carries a current bar, current_price
        comes from it and unrealized_pnl is computed via multiplier.
        Mirrors the production path post-Task-3 for the /M2K position."""
        import zipfile

        from services.api.config import APISettings, get_settings

        # Write a minimal /M2K front-month CSV with last close = 2931.2
        m2k_zip = tmp_path / "future" / "cme" / "daily" / "m2k_trade.zip"
        m2k_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(m2k_zip, "w") as zf:
            zf.writestr(
                "m2k_trade_202606.csv",
                "20260527 00:00,2926.5,2933.5,2920.8,2931.2,4075\n",
            )

        account_id = uuid4()
        positions_rows = [
            {
                "id": uuid4(),
                "market": "/M2K",
                "contract_id": uuid4(),
                "quantity": 1,
                "avg_cost": Decimal("2925.00"),
                "margin_held": Decimal("500.00"),
                "unrealized_pnl": None,
                "last_mark_ts": datetime(2026, 5, 27, 13, 31, tzinfo=UTC),
                "managed_by_version": "a" * 40,
            }
        ]
        repo = _StubRepo(
            account_id=account_id,
            positions_rows=positions_rows,
            nav=Decimal("100000"),
        )
        _bind_repo(override_dep, positions_route, repo)

        original_settings = get_settings()
        override_settings = APISettings(
            **{
                **original_settings.model_dump(),
                "bar_sync_data_root": str(tmp_path),
            }
        )
        override_dep(get_settings, lambda: override_settings)
        response = await api_client.get("/api/positions/current")
        assert response.status_code == 200
        body = response.json()
        m2k = body["positions"][0]
        # 2931.2 from the fixture CSV
        assert Decimal(m2k["current_price"]) == Decimal("2931.2000")
        # (2931.2 - 2925) * 1 * 5 = 31.00 (Decimal multiplier=5 for /M2K)
        assert Decimal(m2k["unrealized_pnl"]) == Decimal("31.00")
        # 31.00 / 100000 = 0.0003 (rounded to 4dp)
        assert Decimal(m2k["unrealized_pnl_pct_of_nav"]) == Decimal("0.0003")

    @pytest.mark.asyncio
    async def test_zero_nav_falls_back_to_zero_pct(
        self,
        api_client: AsyncClient,
        override_dep: Any,
        tmp_path: Any,
    ) -> None:
        """When NAV is unavailable / zero, the pct field returns 0
        without raising a division error. Uses an on-disk MTM fixture
        so unrealized_pnl is non-zero — only the pct path falls back."""
        import zipfile

        from services.api.config import APISettings, get_settings

        m2k_zip = tmp_path / "future" / "cme" / "daily" / "m2k_trade.zip"
        m2k_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(m2k_zip, "w") as zf:
            zf.writestr(
                "m2k_trade_202606.csv",
                "20260527 00:00,2926.5,2933.5,2920.8,2931.2,4075\n",
            )

        account_id = uuid4()
        positions_rows = [
            {
                "id": uuid4(),
                "market": "/M2K",
                "contract_id": uuid4(),
                "quantity": 1,
                "avg_cost": Decimal("2925.00"),
                "margin_held": Decimal("500.00"),
                "unrealized_pnl": None,
                "last_mark_ts": datetime(2026, 5, 27, tzinfo=UTC),
                "managed_by_version": "a" * 40,
            }
        ]
        repo = _StubRepo(
            account_id=account_id,
            positions_rows=positions_rows,
            nav=Decimal("0"),  # zero NAV — must NOT divide
        )
        _bind_repo(override_dep, positions_route, repo)

        original_settings = get_settings()
        override_settings = APISettings(
            **{
                **original_settings.model_dump(),
                "bar_sync_data_root": str(tmp_path),
            }
        )
        override_dep(get_settings, lambda: override_settings)
        response = await api_client.get("/api/positions/current")
        assert response.status_code == 200
        body = response.json()
        # Real unrealized_pnl from MTM ($31), but NAV=0 → pct guard returns 0.
        assert Decimal(body["positions"][0]["unrealized_pnl"]) == Decimal("31.00")
        assert Decimal(body["positions"][0]["unrealized_pnl_pct_of_nav"]) == Decimal("0")


class TestTodayDigestPnlAggregates:
    """Post-2026-05-27 fix: PnL section computes real realized-PnL
    aggregates instead of returning all zeros."""

    @pytest.mark.asyncio
    async def test_pnl_aggregates_propagated_from_repo(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        account_id = uuid4()
        repo = _StubRepo(
            account_id=account_id,
            realized_pnl_aggregates=(
                Decimal("12.34"),  # daily
                Decimal("56.78"),  # weekly
                Decimal("75.80"),  # monthly
                Decimal("75.80"),  # yearly
            ),
        )
        _bind_repo(override_dep, today_route, repo)
        response = await api_client.get("/api/today/digest")
        assert response.status_code == 200
        body = response.json()
        assert Decimal(body["pnl"]["daily_pnl"]) == Decimal("12.34")
        assert Decimal(body["pnl"]["weekly_pnl"]) == Decimal("56.78")
        assert Decimal(body["pnl"]["monthly_pnl"]) == Decimal("75.80")
        assert Decimal(body["pnl"]["yearly_pnl"]) == Decimal("75.80")

    @pytest.mark.asyncio
    async def test_no_account_falls_back_to_zero_pnl(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, today_route, repo)
        response = await api_client.get("/api/today/digest")
        assert response.status_code == 200
        body = response.json()
        assert Decimal(body["pnl"]["daily_pnl"]) == Decimal("0")
        assert Decimal(body["pnl"]["yearly_pnl"]) == Decimal("0")


class TestTodayDigestExposureBreakdown:
    """Post-2026-05-27: exposure section computes per-cluster
    percent-of-NAV + gross/net NAV percentages from positions_current
    rather than returning all zeros. All three fields share the same
    percent-units (0-100) scale so the frontend's ExposureBar math
    (current / limit * 100, with limits like 60 for equity-index)
    renders correctly. Wired via ``services.api.exposure``."""

    @pytest.mark.asyncio
    async def test_no_account_returns_all_zero_exposure(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, today_route, repo)
        response = await api_client.get("/api/today/digest")
        assert response.status_code == 200
        body = response.json()
        assert body["exposure"]["by_cluster"] == {
            "equity_index": "0",
            "commodity": "0",
            "rates_bonds": "0",
            "crypto": "0",
            "fx": "0",
        }
        assert Decimal(body["exposure"]["gross_exposure_pct_nav"]) == Decimal("0")
        assert Decimal(body["exposure"]["net_exposure_pct_nav"]) == Decimal("0")

    @pytest.mark.asyncio
    async def test_mixed_cluster_exposure_aggregated_correctly(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        """Three-position mix exercises long futures (/M2K), short futures
        (/MGC short), and ETF long (TLT). Verifies cluster bucketing,
        gross sum, and signed net sum."""
        account_id = uuid4()
        now = datetime(2026, 5, 27, 13, 31, tzinfo=UTC)
        positions_rows = [
            # /M2K LONG: 1 contract * $2925 * $5/pt = $14,625 notional, equity_index
            {
                "id": uuid4(),
                "market": "/M2K",
                "contract_id": uuid4(),
                "quantity": 1,
                "avg_cost": Decimal("2925.00"),
                "margin_held": Decimal("500"),
                "unrealized_pnl": None,
                "last_mark_ts": now,
                "managed_by_version": "a" * 40,
            },
            # /MGC SHORT: -1 contract * $2400 * $10/pt = $24,000 notional, commodity
            {
                "id": uuid4(),
                "market": "/MGC",
                "contract_id": uuid4(),
                "quantity": -1,
                "avg_cost": Decimal("2400.00"),
                "margin_held": Decimal("500"),
                "unrealized_pnl": None,
                "last_mark_ts": now,
                "managed_by_version": "a" * 40,
            },
            # TLT LONG: 100 shares * $83.72 * 1 = $8,372 notional, rates_bonds
            {
                "id": uuid4(),
                "market": "TLT",
                "contract_id": None,
                "quantity": 100,
                "avg_cost": Decimal("83.72"),
                "margin_held": Decimal("0"),
                "unrealized_pnl": None,
                "last_mark_ts": now,
                "managed_by_version": "a" * 40,
            },
        ]
        repo = _StubRepo(
            account_id=account_id,
            positions_rows=positions_rows,
            nav=Decimal("100000"),
        )
        _bind_repo(override_dep, today_route, repo)
        response = await api_client.get("/api/today/digest")
        assert response.status_code == 200
        body = response.json()
        by_cluster = body["exposure"]["by_cluster"]
        # /M2K: 14625 / 100000 * 100 = 14.625 → 14.62 (ROUND_HALF_EVEN on .X25)
        assert Decimal(by_cluster["equity_index"]) == Decimal("14.62")
        # /MGC abs: 24000 / 100000 * 100 = 24.00
        assert Decimal(by_cluster["commodity"]) == Decimal("24.00")
        # TLT: 8372 / 100000 * 100 = 8.372 → 8.37
        assert Decimal(by_cluster["rates_bonds"]) == Decimal("8.37")
        assert Decimal(by_cluster["crypto"]) == Decimal("0")
        assert Decimal(by_cluster["fx"]) == Decimal("0")
        # Gross = sum of absolute notionals / NAV * 100 = 46997 / 100000 * 100 = 46.997 → 47.00
        assert Decimal(body["exposure"]["gross_exposure_pct_nav"]) == Decimal("47.00")
        # Net = signed sum / NAV * 100 = (14625 - 24000 + 8372) / 100000 * 100 = -1.003 → -1.00
        assert Decimal(body["exposure"]["net_exposure_pct_nav"]) == Decimal("-1.00")

    @pytest.mark.asyncio
    async def test_zero_nav_returns_all_zero_exposure(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        """NAV=0 (or NAV unavailable) → ALL three fields zero. Because
        by_cluster is now a fraction of NAV (percent units), the
        cluster breakdown is undefined when NAV is unavailable; we
        zero it for the same reason gross/net are zeroed."""
        account_id = uuid4()
        positions_rows = [
            {
                "id": uuid4(),
                "market": "/M2K",
                "contract_id": uuid4(),
                "quantity": 1,
                "avg_cost": Decimal("2925"),
                "margin_held": Decimal("0"),
                "unrealized_pnl": None,
                "last_mark_ts": datetime(2026, 5, 27, tzinfo=UTC),
                "managed_by_version": "a" * 40,
            },
        ]
        repo = _StubRepo(
            account_id=account_id,
            positions_rows=positions_rows,
            nav=Decimal("0"),
        )
        _bind_repo(override_dep, today_route, repo)
        response = await api_client.get("/api/today/digest")
        assert response.status_code == 200
        body = response.json()
        assert Decimal(body["exposure"]["by_cluster"]["equity_index"]) == Decimal("0")
        assert Decimal(body["exposure"]["gross_exposure_pct_nav"]) == Decimal("0")
        assert Decimal(body["exposure"]["net_exposure_pct_nav"]) == Decimal("0")


class TestListOrders:
    @pytest.mark.asyncio
    async def test_no_account_returns_empty_envelope(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, orders_route, repo)
        response = await api_client.get("/api/orders")
        assert response.status_code == 200
        assert response.json() == {
            "orders": [],
            "next_cursor": None,
            "has_more": False,
        }

    @pytest.mark.asyncio
    async def test_limit_above_max_rejected_with_422(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=uuid4())
        _bind_repo(override_dep, orders_route, repo)
        response = await api_client.get("/api/orders?limit=999")
        assert response.status_code == 422


class TestListFills:
    @pytest.mark.asyncio
    async def test_no_account_returns_empty_envelope(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, fills_route, repo)
        response = await api_client.get("/api/fills")
        assert response.status_code == 200
        assert response.json() == {
            "fills": [],
            "next_cursor": None,
            "has_more": False,
        }

    @pytest.mark.asyncio
    async def test_populated_fills_mapped_to_fill_objects(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        """Post-2026-05-27 fix: route maps fills JOIN orders rows to
        Fill Pydantic objects with Phase 1 field defaults."""
        account_id = uuid4()
        fill_id = uuid4()
        order_id = uuid4()
        signal_id = uuid4()
        fills_rows = [
            {
                "fill_id": fill_id,
                "order_id": order_id,
                "signal_id": signal_id,
                "market": "/M2K",
                "side": "buy",
                "fill_quantity": 1,
                "fill_price": Decimal("2925.00"),
                "realized_slippage_bps": None,
                "filled_at_utc": datetime(2026, 5, 27, 13, 31, tzinfo=UTC),
            }
        ]
        repo = _StubRepo(
            account_id=account_id,
            fills_page=(fills_rows, None, False),
        )
        _bind_repo(override_dep, fills_route, repo)
        response = await api_client.get("/api/fills")
        assert response.status_code == 200
        body = response.json()
        assert len(body["fills"]) == 1
        f = body["fills"][0]
        assert f["fill_uuid"] == str(fill_id)
        assert f["order_uuid"] == str(order_id)
        assert f["signal_uuid"] == str(signal_id)
        # Phase 1 simplification: instrument_id is the market symbol
        assert f["instrument_id"] == "/M2K"
        assert f["side"] == "buy"
        assert f["qty"] == 1
        assert Decimal(f["price"]) == Decimal("2925.00")
        # realized_slippage_bps=None → 0
        assert Decimal(f["slippage_bps"]) == Decimal("0")
        # Phase 1 placeholder
        assert Decimal(f["expected_price"]) == Decimal("0")


class TestListAlerts:
    @pytest.mark.asyncio
    async def test_no_account_returns_empty_envelope(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, alerts_route, repo)
        response = await api_client.get("/api/alerts")
        assert response.status_code == 200
        assert response.json() == {
            "alerts": [],
            "next_cursor": None,
            "has_more": False,
        }

    @pytest.mark.asyncio
    async def test_status_and_severity_filters_pass_through(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=uuid4())
        _bind_repo(override_dep, alerts_route, repo)
        response = await api_client.get("/api/alerts?status=open&severity=P0&limit=10")
        assert response.status_code == 200
        assert repo.last_alerts_filter == {
            "status": "open",
            "severity": "P0",
            "cursor": None,
            "limit": 10,
        }


# ---------------------------------------------------------------------------
# Trades (Day 26 — backend-spec §4.1.2 / IG §3 Week 7 Tue)
# ---------------------------------------------------------------------------


class TestListTrades:
    @pytest.mark.asyncio
    async def test_no_account_returns_empty_envelope(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, trades_route, repo)
        response = await api_client.get("/api/trades")
        assert response.status_code == 200
        assert response.json() == {
            "trades": [],
            "next_cursor": None,
            "has_more": False,
        }

    @pytest.mark.asyncio
    async def test_filters_pass_through_to_repo(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=uuid4())
        _bind_repo(override_dep, trades_route, repo)
        response = await api_client.get(
            "/api/trades"
            "?from=2026-05-01T00:00:00Z"
            "&to=2026-05-19T23:59:59Z"
            "&market=%2FMES"  # /MES URL-encoded
            "&state=closed"
            "&id_prefix=0190a1b2"
            "&limit=25",
        )
        assert response.status_code == 200
        assert repo.last_trades_filter is not None
        assert repo.last_trades_filter["market"] == "/MES"
        assert repo.last_trades_filter["state"] == "closed"
        assert repo.last_trades_filter["id_prefix"] == "0190a1b2"
        assert repo.last_trades_filter["limit"] == 25
        assert repo.last_trades_filter["from_utc"] == datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
        assert repo.last_trades_filter["to_utc"] == datetime(2026, 5, 19, 23, 59, 59, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_limit_above_max_rejected_with_422(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=uuid4())
        _bind_repo(override_dep, trades_route, repo)
        response = await api_client.get("/api/trades?limit=999")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_populated_row_maps_to_summary_with_short_hash(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        # Day 26 has no real trades in the DB, but the mapper still needs
        # coverage so the wire shape is locked. Stub a single populated row
        # and assert the response body matches the locked TradeSummary
        # shape — including managed_by_strategy_version being the first 7
        # chars of the 40-char managed_by_version.
        full_hash = "a" * 40
        trade_id = uuid4()
        signal_id = uuid4()
        opened = datetime(2026, 5, 19, 17, 32, 5, tzinfo=UTC)
        closed = datetime(2026, 5, 19, 21, 11, 12, tzinfo=UTC)
        repo = _StubRepo(
            account_id=uuid4(),
            trades_page=(
                [
                    {
                        "id": trade_id,
                        "env": "paper",
                        "market": "/MES",
                        "direction": "long",
                        "state": "closed",
                        "opened_at_utc": opened,
                        "closed_at_utc": closed,
                        "total_quantity": 1,
                        "avg_entry_price": Decimal("5234.50"),
                        "avg_exit_price": Decimal("5237.25"),
                        "realized_pnl_usd": Decimal("13.75"),
                        "entry_signal_id": signal_id,
                        "managed_by_version": full_hash,
                    }
                ],
                None,
                False,
            ),
        )
        _bind_repo(override_dep, trades_route, repo)
        response = await api_client.get("/api/trades")
        assert response.status_code == 200
        body = response.json()
        assert len(body["trades"]) == 1
        row = body["trades"][0]
        assert row["id"] == str(trade_id)
        assert row["entry_signal_id"] == str(signal_id)
        assert row["env"] == "paper"
        assert row["market"] == "/MES"
        assert row["direction"] == "long"
        assert row["state"] == "closed"
        assert row["total_quantity"] == 1
        assert row["avg_entry_price"] == "5234.50"
        assert row["avg_exit_price"] == "5237.25"
        assert row["realized_pnl_usd"] == "13.75"
        # 7-char short hash matches git rev-parse --short default.
        assert row["managed_by_strategy_version"] == "aaaaaaa"
        assert len(row["managed_by_strategy_version"]) == 7


class TestTradeDetail:
    @pytest.mark.asyncio
    async def test_no_account_returns_404_envelope(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        # When no account exists, every trade lookup is a 404 — the spec
        # says "TRADE_NOT_FOUND" is the canonical error code so the frontend
        # /trades/:id page can render a graceful "Trade not found" body
        # instead of Next's hard 404 (IG line 417: "ensures Discord
        # deep-links don't 404").
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, trades_route, repo)
        tid = uuid4()
        response = await api_client.get(f"/api/trades/{tid}")
        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == "TRADE_NOT_FOUND"
        assert body["details"] == {"trade_id": str(tid)}

    @pytest.mark.asyncio
    async def test_missing_trade_returns_404_envelope(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=uuid4(), trade_by_id=None)
        _bind_repo(override_dep, trades_route, repo)
        tid = uuid4()
        response = await api_client.get(f"/api/trades/{tid}")
        assert response.status_code == 404
        assert response.json()["error_code"] == "TRADE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_malformed_uuid_rejected_with_422(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        # FastAPI's dependency resolver enters generator-based deps before
        # path-param validation finishes, so without overriding ``_get_repo``
        # the real ``get_session`` raises ``DB pool not initialized``.
        # Override with a stub repo even though it's never called — the
        # UUID parse failure surfaces as 422 instead.
        repo = _StubRepo(account_id=uuid4())
        _bind_repo(override_dep, trades_route, repo)
        response = await api_client.get("/api/trades/not-a-uuid")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_populated_row_maps_to_detail_shape(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        full_hash = "b" * 40
        trade_id = uuid4()
        account_id = uuid4()
        signal_id = uuid4()
        entry_oid = uuid4()
        exit_oid = uuid4()
        slip_vid = uuid4()
        opened = datetime(2026, 5, 19, 17, 32, 5, tzinfo=UTC)
        closed = datetime(2026, 5, 19, 21, 11, 12, tzinfo=UTC)
        row = {
            "id": trade_id,
            "account_id": account_id,
            "env": "paper",
            "market": "/MES",
            "direction": "long",
            "state": "closed",
            "opened_at_utc": opened,
            "closed_at_utc": closed,
            "total_quantity": 1,
            "avg_entry_price": Decimal("5234.50"),
            "avg_exit_price": Decimal("5237.25"),
            "realized_pnl_usd": Decimal("13.75"),
            "realized_commission_usd": Decimal("0.85"),
            "dividend_pnl_usd": Decimal("0"),
            "entry_signal_id": signal_id,
            "entry_order_id": entry_oid,
            "exit_order_id": exit_oid,
            "managed_by_version": full_hash,
            "strategy_hash": "c" * 40,
            "parameter_set_hash": "d" * 64,
            "slippage_calibration_version_id": slip_vid,
        }
        repo = _StubRepo(account_id=account_id, trade_by_id=row)
        _bind_repo(override_dep, trades_route, repo)
        response = await api_client.get(f"/api/trades/{trade_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(trade_id)
        assert body["account_id"] == str(account_id)
        assert body["entry_signal_id"] == str(signal_id)
        assert body["entry_order_id"] == str(entry_oid)
        assert body["exit_order_id"] == str(exit_oid)
        assert body["slippage_calibration_version_id"] == str(slip_vid)
        assert body["managed_by_version"] == full_hash
        assert body["managed_by_strategy_version"] == "bbbbbbb"
        assert body["strategy_hash"] == "c" * 40
        assert body["parameter_set_hash"] == "d" * 64
        assert body["realized_pnl_usd"] == "13.75"
        assert body["realized_commission_usd"] == "0.85"
        assert body["dividend_pnl_usd"] == "0"
        # repo received the parsed UUID
        assert repo.last_trade_by_id_arg == trade_id

    @pytest.mark.asyncio
    async def test_open_position_detail_keeps_nullable_fields_null(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        # An open_position trade has no closed_at_utc / avg_exit_price /
        # realized_pnl_usd / exit_order_id. The detail page renders these
        # as "—" placeholders; the wire contract must allow null.
        trade_id = uuid4()
        account_id = uuid4()
        signal_id = uuid4()
        entry_oid = uuid4()
        slip_vid = uuid4()
        opened = datetime(2026, 5, 19, 17, 32, 5, tzinfo=UTC)
        row = {
            "id": trade_id,
            "account_id": account_id,
            "env": "paper",
            "market": "/MES",
            "direction": "long",
            "state": "open_position",
            "opened_at_utc": opened,
            "closed_at_utc": None,
            "total_quantity": 2,
            "avg_entry_price": Decimal("5234.50"),
            "avg_exit_price": None,
            "realized_pnl_usd": None,
            "realized_commission_usd": Decimal("0"),
            "dividend_pnl_usd": Decimal("0"),
            "entry_signal_id": signal_id,
            "entry_order_id": entry_oid,
            "exit_order_id": None,
            "managed_by_version": "e" * 40,
            "strategy_hash": "f" * 40,
            "parameter_set_hash": "9" * 64,
            "slippage_calibration_version_id": slip_vid,
        }
        repo = _StubRepo(account_id=account_id, trade_by_id=row)
        _bind_repo(override_dep, trades_route, repo)
        response = await api_client.get(f"/api/trades/{trade_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "open_position"
        assert body["closed_at_utc"] is None
        assert body["avg_exit_price"] is None
        assert body["realized_pnl_usd"] is None
        assert body["exit_order_id"] is None


# ---------------------------------------------------------------------------
# Cursor pagination helper
# ---------------------------------------------------------------------------


class TestCursorPagination:
    def test_encode_decode_round_trip(self) -> None:
        ts = datetime(2026, 5, 10, 17, 30, 45, 123456, tzinfo=UTC)
        row_id = "0190a1b2-c3d4-7000-8000-000000000001"
        cursor = encode_cursor(ts, row_id)
        decoded_ts, decoded_id = decode_cursor(cursor)
        assert decoded_ts == ts
        assert decoded_id == row_id

    def test_invalid_cursor_raises_app_error(self) -> None:
        from services.api.errors import AppError

        with pytest.raises(AppError) as exc:
            decode_cursor("not-a-real-cursor-value!!!")
        assert exc.value.error_code == "INVALID_CURSOR"
        assert exc.value.status_code == 400

    def test_clamp_limit_returns_default_when_none(self) -> None:
        assert clamp_limit(None) == 50

    def test_clamp_limit_caps_at_max(self) -> None:
        assert clamp_limit(500) == 200

    def test_clamp_limit_floors_at_min(self) -> None:
        assert clamp_limit(0) == 1


# ---------------------------------------------------------------------------
# Day 27 — Risk envelope (GET /api/system/risk-envelope)
# ---------------------------------------------------------------------------


class TestRiskEnvelope:
    @pytest.mark.asyncio
    async def test_empty_parameters_table_returns_spec_defaults(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(parameter_set_row=None)
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/risk-envelope")
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "spec_defaults"
        assert body["parameter_set_hash"] is None
        assert body["parameter_set_active_since_utc"] is None
        # 15 locked rows (5 portfolio + 5 cluster + 2 correlation + 3 DD)
        assert len(body["items"]) == 15
        # Verify a few canonical entries are present + Decimal-as-string
        keys = {item["key"] for item in body["items"]}
        assert "vol_target_annual" in keys
        assert "cluster_cap_equity_index" in keys
        assert "correlation_halt_threshold" in keys
        assert "monthly_drawdown_vol_halve" in keys
        vol_target = next(i for i in body["items"] if i["key"] == "vol_target_annual")
        assert vol_target["value"] == "0.14"
        assert vol_target["unit"] == "percent"
        assert vol_target["cluster"] is None
        equity_cluster = next(i for i in body["items"] if i["key"] == "cluster_cap_equity_index")
        assert equity_cluster["cluster"] == "equity_index"

    @pytest.mark.asyncio
    async def test_populated_parameter_set_uses_table_values(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        # All 15 canonical keys present + a couple of non-default values to
        # prove the route reads from the table when populated.
        parameters_jsonb: dict[str, Any] = {
            "vol_target_annual": "0.12",
            "per_position_target": "0.25",
            "per_position_hard_floor": "0.50",
            "gross_exposure_cap": "2.50",
            "net_exposure_cap": "1.50",
            "cluster_cap_equity_index": "0.55",
            "cluster_cap_rates_bonds": "0.80",
            "cluster_cap_commodity": "0.80",
            "cluster_cap_crypto": "0.40",
            "cluster_cap_fx": "0.30",
            "correlation_alert_threshold": "0.70",
            "correlation_halt_threshold": "0.85",
            "daily_loss_limit": "-0.05",
            "trailing_drawdown_limit": "-0.20",
            "monthly_drawdown_vol_halve": "-0.10",
        }
        active_since = datetime(2026, 4, 1, tzinfo=UTC)
        repo = _StubRepo(
            parameter_set_row=ParameterSetRow(
                parameter_set_hash="a" * 64,
                parameters=parameters_jsonb,
                first_active_at=active_since,
                last_active_at=None,
            )
        )
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/risk-envelope")
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "parameters_table"
        assert body["parameter_set_hash"] == "a" * 64
        assert body["parameter_set_active_since_utc"].startswith("2026-04-01")
        # Tweaked values surface verbatim (Decimal -> string)
        vol_target = next(i for i in body["items"] if i["key"] == "vol_target_annual")
        assert vol_target["value"] == "0.12"
        gross = next(i for i in body["items"] if i["key"] == "gross_exposure_cap")
        assert gross["value"] == "2.50"

    @pytest.mark.asyncio
    async def test_partial_parameter_set_falls_back_to_defaults(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        # Row exists but missing required keys -> route falls back to spec
        # defaults rather than rendering a partially-filled envelope.
        repo = _StubRepo(
            parameter_set_row=ParameterSetRow(
                parameter_set_hash="b" * 64,
                parameters={"vol_target_annual": "0.10"},  # only 1 of 15
                first_active_at=datetime(2026, 4, 1, tzinfo=UTC),
                last_active_at=None,
            )
        )
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/risk-envelope")
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "spec_defaults"
        assert body["parameter_set_hash"] is None


# ---------------------------------------------------------------------------
# Day 27 — Audit explorer (GET /api/system/audit)
# ---------------------------------------------------------------------------


def _make_audit_row(
    *,
    seq: int,
    event_type: str = "state_transition_normal_to_halt",
    env: str = "paper",
    payload: bytes = b'{"reason":"trailing_dd_breach","triggered_by":"operator"}',
) -> AuditLogRow:
    now = datetime(2026, 5, 18, 13, 30, tzinfo=UTC)
    return AuditLogRow(
        event_uuid=uuid4(),
        sequence_no=seq,
        event_type=event_type,
        env=env,
        phase_at_emit=0,
        source_clock_ts=now,
        ingest_clock_ts=now,
        prev_hash=b"\x00" * 32,
        record_hash=b"\xab" * 32,
        payload_jcs=payload,
        repaired_for_sequence_no=None,
        repaired_for_event_timestamp=None,
    )


class TestAuditLogTable:
    @pytest.mark.asyncio
    async def test_empty_table_returns_empty_page(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(audit_log_page=([], None, False))
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/audit")
        assert response.status_code == 200
        body = response.json()
        assert body == {"entries": [], "next_cursor": None, "has_more": False}
        assert repo.last_audit_filter == {
            "event_type": None,
            "env": None,
            "from_ingest_utc": None,
            "to_ingest_utc": None,
            "before_sequence_no": None,
            "limit": 50,
        }

    @pytest.mark.asyncio
    async def test_filters_pass_through_to_repo(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(audit_log_page=([], None, False))
        _bind_repo(override_dep, system_route, repo)
        url = (
            "/api/system/audit?"
            "event_type=state_transition_normal_to_halt&"
            "env=paper&"
            "from=2026-05-01T00:00:00%2B00:00&"
            "to=2026-05-20T23:59:59%2B00:00&"
            "limit=25"
        )
        response = await api_client.get(url)
        assert response.status_code == 200
        f = repo.last_audit_filter
        assert f is not None
        assert f["event_type"] == "state_transition_normal_to_halt"
        assert f["env"] == "paper"
        assert f["from_ingest_utc"].year == 2026
        assert f["to_ingest_utc"].day == 20
        assert f["limit"] == 25
        assert f["before_sequence_no"] is None

    @pytest.mark.asyncio
    async def test_populated_row_maps_to_wire_entry(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        row = _make_audit_row(seq=1)
        repo = _StubRepo(audit_log_page=([row], None, False))
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/audit")
        assert response.status_code == 200
        body = response.json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        assert len(body["entries"]) == 1
        entry = body["entries"][0]
        assert entry["sequence_no"] == 1
        assert entry["event_type"] == "state_transition_normal_to_halt"
        assert entry["env"] == "paper"
        assert entry["prev_hash_hex"] == "00" * 32
        assert entry["record_hash_hex"] == "ab" * 32
        assert entry["payload_preview"].startswith('{"reason":"trailing_dd_breach"')
        # Preview capped at 80 chars
        assert len(entry["payload_preview"]) <= 80

    @pytest.mark.asyncio
    async def test_has_more_emits_next_cursor(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        row = _make_audit_row(seq=42)
        repo = _StubRepo(audit_log_page=([row], 42, True))
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/audit?limit=1")
        assert response.status_code == 200
        body = response.json()
        assert body["has_more"] is True
        assert body["next_cursor"] == "42"

    @pytest.mark.asyncio
    async def test_cursor_passthrough(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(audit_log_page=([], None, False))
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/audit?cursor=100")
        assert response.status_code == 200
        assert repo.last_audit_filter is not None
        assert repo.last_audit_filter["before_sequence_no"] == 100

    @pytest.mark.asyncio
    async def test_malformed_cursor_returns_invalid_cursor(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo()
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/audit?cursor=not-an-int")
        assert response.status_code == 400
        body = response.json()
        assert body["error_code"] == "INVALID_CURSOR"

    @pytest.mark.asyncio
    async def test_zero_cursor_rejected(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo()
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/audit?cursor=0")
        assert response.status_code == 400
        body = response.json()
        assert body["error_code"] == "INVALID_CURSOR"

    @pytest.mark.asyncio
    async def test_limit_over_max_returns_422(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo()
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/audit?limit=999")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Day 27 — Watchdog status (GET /api/system/watchdog)
# ---------------------------------------------------------------------------


class TestWatchdogStatus:
    @pytest.mark.asyncio
    async def test_no_account_returns_never_pinged(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=None)
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/watchdog")
        assert response.status_code == 200
        body = response.json()
        assert body["has_ever_pinged"] is False
        assert body["last_ping_utc"].startswith("1970-01-01")
        assert body["seconds_since_last_ping"] is None
        assert body["watchdog_id"] is None
        assert body["consecutive_failures_observed"] is None

    @pytest.mark.asyncio
    async def test_recent_ping_emits_age(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        account_id = uuid4()
        ping = datetime.now(tz=UTC) - timedelta(seconds=42)
        repo = _StubRepo(account_id=account_id, watchdog_ping=ping)
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/watchdog")
        assert response.status_code == 200
        body = response.json()
        assert body["has_ever_pinged"] is True
        # Age within a tolerance window — the route uses datetime.now()
        # internally, so we can't pin to an exact int.
        assert body["seconds_since_last_ping"] is not None
        assert 40 <= body["seconds_since_last_ping"] <= 60

    @pytest.mark.asyncio
    async def test_account_exists_but_no_pings_yet(
        self,
        api_client: AsyncClient,
        override_dep: Any,
    ) -> None:
        repo = _StubRepo(account_id=uuid4(), watchdog_ping=None)
        _bind_repo(override_dep, system_route, repo)
        response = await api_client.get("/api/system/watchdog")
        assert response.status_code == 200
        body = response.json()
        assert body["has_ever_pinged"] is False
        assert body["last_ping_utc"].startswith("1970-01-01")
