"""Unit tests for the api lifespan's OrderPlacementWorker bring-up helpers.

Worker-PR-1 follow-up (post-pivot 2026-05-12). The lifespan adds an
api-resident background task that drains approved signals into IBKR.
Startup is best-effort: a missing accounts row, ib_async-import failure,
or ib_gateway unreachability should all skip worker startup with a
warning rather than crash the api.

Pure-policy tests:
  * ``_audit_env_from_settings`` — degrades unknown env to ``paper``.
  * ``_start_order_placement_worker`` — disabled-via-setting short
    circuits without touching the DB; the lifespan also catches any
    exception from the helper so worker bring-up failures don't kill
    the api.

The end-to-end "worker successfully places an order" path is covered
elsewhere (Worker-PR-1's own test suite + the Phase 5 ceremony).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(scope="module", autouse=True)
def _stub_api_env() -> Any:
    """Inject stub env vars before importing services.api.main.

    The module's bottom-line `app = create_app()` runs at import time +
    calls `get_settings()` which requires `API_DATABASE_URL`. The
    fixture sets the env vars module-wide so the import-time settings
    resolution succeeds; nothing in this test actually exercises the
    DB.
    """
    import os

    overrides = {
        "API_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        "API_ENVIRONMENT": "dev",
        "API_VERSION": "test",
        "API_LOG_LEVEL": "INFO",
    }
    previous: dict[str, str | None] = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    # Clear pydantic settings cache so the override env vars take.
    from services.api import config as api_config

    api_config.get_settings.cache_clear()

    yield

    for k, prev in previous.items():
        if prev is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = prev
    api_config.get_settings.cache_clear()


def _settings(**overrides: Any) -> Any:
    """Lightweight SimpleNamespace stand-in for APISettings.

    Tests construct with the fields the helpers read; everything else is
    untouched. This avoids the full pydantic-settings construction (which
    requires database_url + env-var plumbing).
    """
    base = SimpleNamespace(
        environment="paper",
        order_placement_worker_enabled=True,
        ibkr_host="ib_gateway",
        ibkr_port=4004,
        ibkr_client_id=1,
        ibkr_account="DUQ_TEST",
        order_placement_poll_interval_seconds=5.0,
        reconciliation_scheduler_enabled=True,
        flex_query_id=12345,
        flex_query_token=None,  # SecretStr in real settings; helper handles None
        # PR for follow-up #2 — alert_dispatch_hook construction reads these.
        # Default None means the hook is skipped (the operator-friendly
        # pre-sops-population state).
        discord_webhook_url_alerts=None,
        discord_webhook_url_critical=None,
        resend_api_key=None,
        resend_from_address=None,
        resend_to_address=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class _SecretStub:
    """Lightweight SecretStr stand-in carrying a `.get_secret_value()` method.

    Real `APISettings` fields are pydantic SecretStr; tests don't go through
    pydantic-settings construction, so we provide the .get_secret_value
    contract here.
    """

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class TestAuditEnvFromSettings:
    def test_paper_passes_through(self) -> None:
        from services.api import main as api_main

        assert api_main._audit_env_from_settings(_settings(environment="paper")) == "paper"

    def test_live_small_passes_through(self) -> None:
        from services.api import main as api_main

        assert (
            api_main._audit_env_from_settings(_settings(environment="live-small")) == "live-small"
        )

    def test_live_scale_passes_through(self) -> None:
        from services.api import main as api_main

        assert (
            api_main._audit_env_from_settings(_settings(environment="live-scale")) == "live-scale"
        )

    def test_dev_degrades_to_paper(self) -> None:
        """`dev` is not a valid audit_log.env value; degrade to paper."""
        from services.api import main as api_main

        assert api_main._audit_env_from_settings(_settings(environment="dev")) == "paper"


class TestStartOrderPlacementWorkerDisabled:
    @pytest.mark.asyncio
    async def test_disabled_setting_returns_none(self) -> None:
        """When `order_placement_worker_enabled=False`, the helper short-circuits."""
        from services.api import main as api_main

        result = await api_main._start_order_placement_worker(
            _settings(order_placement_worker_enabled=False)
        )
        assert result is None


class TestStopOrderPlacementWorkerNoState:
    @pytest.mark.asyncio
    async def test_none_state_is_idempotent(self) -> None:
        """Lifespan calls _stop_order_placement_worker(None) when start returned None."""
        from services.api import main as api_main

        # Should not raise.
        await api_main._stop_order_placement_worker(None)


class TestStartReconciliationSchedulerDefensive:
    """Recon scheduler bring-up is gated on operator-populated sops fields.

    These tests lock the contract: when the operator hasn't populated
    `ibkr.flex_query_id` + `ibkr.flex_query_token` yet (the Phase-1
    expected state at deploy time), the scheduler quietly does not
    start. Once the operator updates sops + restarts the api, the
    real-cred path takes over.
    """

    @pytest.mark.asyncio
    async def test_disabled_setting_returns_none(self) -> None:
        from services.api import main as api_main

        result = await api_main._start_reconciliation_scheduler(
            _settings(reconciliation_scheduler_enabled=False)
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_flex_id_returns_none(self) -> None:
        from services.api import main as api_main

        result = await api_main._start_reconciliation_scheduler(
            _settings(flex_query_id=None, flex_query_token=SimpleNamespace())
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_flex_token_returns_none(self) -> None:
        from services.api import main as api_main

        result = await api_main._start_reconciliation_scheduler(
            _settings(flex_query_id=123, flex_query_token=None)
        )
        assert result is None


class TestStopReconciliationSchedulerNoState:
    @pytest.mark.asyncio
    async def test_none_state_is_idempotent(self) -> None:
        from services.api import main as api_main

        # Lifespan calls _stop_reconciliation_scheduler(None) when start returned None.
        await api_main._stop_reconciliation_scheduler(None)


class TestEntrypointFlexQueryMapping:
    """Tests for sops yaml → API_FLEX_QUERY_ID + API_FLEX_QUERY_TOKEN mapping."""

    def test_flex_query_credentials_mapped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  paper_account: DUQ_TEST\n"
            "  flex_query_id: 991122\n"
            "  flex_query_token: tok-redacted\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_FLEX_QUERY_ID", raising=False)
        monkeypatch.delenv("API_FLEX_QUERY_TOKEN", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])

        import os

        assert os.environ["API_FLEX_QUERY_ID"] == "991122"
        assert os.environ["API_FLEX_QUERY_TOKEN"] == "tok-redacted"

    def test_placeholder_flex_credentials_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  paper_account: DUQ_TEST\n"
            "  flex_query_id: <TODO_FLEX_QUERY_ID>\n"
            "  flex_query_token: <TODO_FLEX_QUERY_TOKEN>\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_FLEX_QUERY_ID", raising=False)
        monkeypatch.delenv("API_FLEX_QUERY_TOKEN", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            pass

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])

        import os

        assert "API_FLEX_QUERY_ID" not in os.environ
        assert "API_FLEX_QUERY_TOKEN" not in os.environ


class TestEntrypointIbkrAccountMapping:
    """Tests for the sops yaml → API_IBKR_ACCOUNT mapping in entrypoint.py."""

    def test_paper_env_maps_paper_account(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  paper_account: DUQ_TEST_PAPER\n"
            "  live_account: U_TEST_LIVE\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_IBKR_ACCOUNT", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        # Stub execvp so main() returns instead of replacing the process.
        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd
            called["argv"] = argv

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])

        assert "cmd" in called  # exec was invoked
        # The sops-sourced paper_account should have been promoted.
        import os

        assert os.environ["API_IBKR_ACCOUNT"] == "DUQ_TEST_PAPER"

    def test_live_env_maps_live_account(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  paper_account: DUQ_PAPER\n"
            "  live_account: U_TEST_LIVE\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_IBKR_ACCOUNT", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "live-small")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd
            called["argv"] = argv

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])

        assert "cmd" in called
        import os

        assert os.environ["API_IBKR_ACCOUNT"] == "U_TEST_LIVE"

    def test_missing_ibkr_block_leaves_account_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        # No ibkr block in the sops yaml.
        yaml_text = "postgres:\n  app_service_password: hexpwd\n"
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_IBKR_ACCOUNT", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd
            called["argv"] = argv

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])

        assert "cmd" in called
        import os

        assert "API_IBKR_ACCOUNT" not in os.environ

    def test_placeholder_value_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  paper_account: <TODO_FROM_DAY_28_PIVOT>\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_IBKR_ACCOUNT", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd
            called["argv"] = argv

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])

        assert "cmd" in called
        import os

        assert "API_IBKR_ACCOUNT" not in os.environ

    def test_account_number_canonical_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """`ibkr.account_number` is the canonical key — read it first."""
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  account_number: DUQ_CANONICAL\n"
            "  paper_account: DUQ_LEGACY_PAPER\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_IBKR_ACCOUNT", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        import os

        assert os.environ["API_IBKR_ACCOUNT"] == "DUQ_CANONICAL"

    def test_account_number_works_for_live_env_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """Canonical key is env-agnostic — same field for paper + live."""
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n  app_service_password: hexpwd\nibkr:\n  account_number: U_CANONICAL\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_IBKR_ACCOUNT", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "live-small")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        import os

        assert os.environ["API_IBKR_ACCOUNT"] == "U_CANONICAL"

    def test_falls_back_to_paper_account_when_canonical_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """When `account_number` is absent, fall back to per-env legacy keys."""
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n  app_service_password: hexpwd\nibkr:\n  paper_account: DUQ_FALLBACK\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_IBKR_ACCOUNT", raising=False)
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        import os

        assert os.environ["API_IBKR_ACCOUNT"] == "DUQ_FALLBACK"


# ---------------------------------------------------------------------------
# PR for follow-up #2 — alert_dispatch_hook construction
# ---------------------------------------------------------------------------


class TestBuildAlertDispatchHook:
    """``_build_alert_dispatch_hook`` returns a closure or None.

    None when sops Discord URLs aren't populated (the operator hasn't
    populated `discord.webhook_urls.alerts` yet). Closure when at least
    `discord_webhook_url_alerts` is set; the closure runs INSERT alerts
    + dispatch_alert when fired.

    Email identity is independent: present iff ALL THREE Resend fields
    populated. Missing any → email_identity stays None (the dispatcher
    will trip on a P0 alert; P2 alerts unaffected).
    """

    def test_no_discord_alerts_url_returns_none(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_alert_dispatch_hook(_settings())  # all None
        assert hook is None

    def test_with_alerts_url_returns_callable(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_alert_dispatch_hook(
            _settings(discord_webhook_url_alerts=_SecretStub("https://discord.test/a"))
        )
        assert hook is not None
        assert callable(hook)

    def test_with_critical_url_includes_in_webhook_map(self) -> None:
        # End-to-end: the closure when fired should include #critical
        # in its webhook_urls map. We don't actually fire the closure
        # here (that needs an in-process DB); the integration test in
        # the next class spies on the captured webhook_urls dict.
        from services.api import main as api_main

        hook = api_main._build_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                discord_webhook_url_critical=_SecretStub("https://discord.test/c"),
            )
        )
        assert hook is not None

    def test_partial_resend_config_skips_email_identity(self) -> None:
        # Missing to_address → email_identity stays None → P0 dispatch
        # would trip the planner; P2 unaffected.
        from services.api import main as api_main

        hook = api_main._build_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                resend_api_key=_SecretStub("re_test"),
                resend_from_address="from@test",
                # to_address still None
            )
        )
        # Returns a callable but email_identity will be None inside the
        # closure (closes over None).
        assert hook is not None

    def test_full_resend_config_constructs_email_identity(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                discord_webhook_url_critical=_SecretStub("https://discord.test/c"),
                resend_api_key=_SecretStub("re_test"),
                resend_from_address="from@test",
                resend_to_address="to@test",
            )
        )
        assert hook is not None


class TestAlertDispatchHookFired:
    """The hook closure: INSERTs alerts row + invokes dispatch_alert.

    Two-session lifecycle: one session for the INSERT, one for the
    dispatcher's read+UPDATE. Both come from a stub session_factory.
    """

    @pytest.mark.asyncio
    async def test_hook_inserts_then_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.reconciliation.apply import AlertDispatchContext
        from services.reconciliation.recon import AlertDescriptor

        # Stub session_factory: two sessions get opened; the first INSERTs
        # + RETURNING the alert id, the second is consumed by dispatch_alert.
        new_alert_id = uuid4()

        sessions_opened: list[MagicMock] = []

        def factory() -> Any:
            session = MagicMock()
            sessions_opened.append(session)

            async def execute(stmt: Any, params: Any = None) -> Any:
                from types import SimpleNamespace as _NS

                result = MagicMock()
                result.fetchone = MagicMock(return_value=_NS(id=new_alert_id))
                return result

            async def commit() -> None:
                return None

            session.execute = execute  # type: ignore[method-assign]
            session.commit = commit  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        # api_db.get_session_factory returns the factory.
        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        # Stub dispatch_alert so we don't actually fan out to Discord.
        dispatched: dict[str, Any] = {}

        async def fake_dispatch(
            *,
            session: Any,
            alert_id: Any,
            http_client: Any,
            webhook_urls: Any,
            email_identity: Any,
        ) -> Any:
            dispatched["alert_id"] = alert_id
            dispatched["webhook_urls"] = dict(webhook_urls)
            dispatched["email_identity"] = email_identity
            return MagicMock(short_circuited=False, delivery_status={"discord_alerts": "ok"})

        # Patch where main.py looks it up (lazy-imported inside _build_alert_dispatch_hook).
        from services.webhook_pusher import dispatcher as wpd

        monkeypatch.setattr(wpd, "dispatch_alert", fake_dispatch)

        hook = api_main._build_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                discord_webhook_url_critical=_SecretStub("https://discord.test/c"),
                resend_api_key=_SecretStub("re_test"),
                resend_from_address="from@test",
                resend_to_address="to@test",
            )
        )
        assert hook is not None

        ctx = AlertDispatchContext(
            descriptor=AlertDescriptor(
                triggering_break_index=0,
                severity="P0",
                category="reconciliation_break",
                title="Reconciliation break: cash divergence $1500.00",
                body="EOD reconciliation detected a cash divergence.\nDelta: 1500.00",
                payload={
                    "metric": "cash_usd",
                    "market": None,
                    "delta": "1500.00",
                    "tolerance": "5.00",
                },
            ),
            triggering_audit_event_uuid=uuid4(),
            account_id=uuid4(),
            env="paper",
        )

        await hook(ctx)

        # INSERT happened (1 session).
        assert len(sessions_opened) >= 1
        # Dispatch happened with the right shape.
        assert dispatched["alert_id"] == new_alert_id
        assert "discord_alerts" in dispatched["webhook_urls"]
        assert "discord_critical" in dispatched["webhook_urls"]
        assert dispatched["email_identity"] is not None
        # Email identity carries the from/to/api_key.
        ei = dispatched["email_identity"]
        assert ei.from_address == "from@test"
        assert ei.to_address == "to@test"
        assert ei.resend_api_key == "re_test"

    @pytest.mark.asyncio
    async def test_hook_skips_email_when_resend_unwired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # P2-only path: only #alerts URL set; no Resend → email_identity
        # stays None when the closure passes it to dispatch_alert.
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.reconciliation.apply import AlertDispatchContext
        from services.reconciliation.recon import AlertDescriptor

        def factory() -> Any:
            session = MagicMock()

            async def execute(stmt: Any, params: Any = None) -> Any:
                from types import SimpleNamespace as _NS

                result = MagicMock()
                result.fetchone = MagicMock(return_value=_NS(id=uuid4()))
                return result

            async def commit() -> None:
                return None

            session.execute = execute  # type: ignore[method-assign]
            session.commit = commit  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        dispatched: dict[str, Any] = {}

        async def fake_dispatch(
            *,
            session: Any,
            alert_id: Any,
            http_client: Any,
            webhook_urls: Any,
            email_identity: Any,
        ) -> Any:
            dispatched["email_identity"] = email_identity
            return MagicMock(short_circuited=False, delivery_status={"discord_alerts": "ok"})

        from services.webhook_pusher import dispatcher as wpd

        monkeypatch.setattr(wpd, "dispatch_alert", fake_dispatch)

        hook = api_main._build_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                # Only api_key set, missing from/to → email_identity None.
                resend_api_key=_SecretStub("re_test"),
            )
        )
        assert hook is not None
        ctx = AlertDispatchContext(
            descriptor=AlertDescriptor(
                triggering_break_index=0,
                severity="P2",
                category="reconciliation_break",
                title="Reconciliation break: /MES position divergence 1 contract",
                body="EOD reconciliation detected a position divergence on /MES.",
                payload={"metric": "position_qty", "market": "/MES", "delta": "1"},
            ),
            triggering_audit_event_uuid=uuid4(),
            account_id=uuid4(),
            env="paper",
        )
        await hook(ctx)
        assert dispatched["email_identity"] is None


class TestBuildStateTransitionHook:
    """PR-J: ``_build_state_transition_hook`` returns a callable closure.

    Phase 1 always returns a hook (no opt-out setting yet). The
    closure reads risk_state, plans the transition, applies it, then
    emits SSE. We exercise each branch with mocked DB sessions +
    mocked state-machine internals.
    """

    def test_returns_callable(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_state_transition_hook(_settings())
        assert hook is not None
        assert callable(hook)


class TestStateTransitionHookFired:
    """The PR-J hook closure: read risk_state → plan → apply → SSE."""

    @pytest.mark.asyncio
    async def test_already_halted_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If risk_state is already HALT_NEW, the hook logs + returns
        without invoking plan_invoke_kill_switch."""
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.reconciliation.apply import ReconciliationKillSwitchContext

        def factory() -> Any:
            session = MagicMock()

            async def execute(stmt: Any, params: Any = None) -> Any:
                from types import SimpleNamespace as _NS

                result = MagicMock()
                # risk_state already HALT_NEW.
                result.fetchone = MagicMock(
                    return_value=_NS(
                        state="HALT_NEW",
                        severity="routine",
                        convalescent_session_count=0,
                    )
                )
                return result

            session.execute = execute  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        plan_calls: list[Any] = []

        def fake_plan(*args: Any, **kwargs: Any) -> Any:
            plan_calls.append(kwargs)
            raise AssertionError("plan_invoke_kill_switch should NOT be called when already halted")

        from services.risk import state_machine as sm

        monkeypatch.setattr(sm, "plan_invoke_kill_switch", fake_plan)

        hook = api_main._build_state_transition_hook(_settings())
        assert hook is not None

        ctx = ReconciliationKillSwitchContext(
            account_id=uuid4(),
            env="paper",
            actionable_break_count=1,
            primary_audit_event_uuid=uuid4(),
        )
        # Should NOT raise; should NOT call plan_invoke_kill_switch.
        await hook(ctx)
        assert plan_calls == []

    @pytest.mark.asyncio
    async def test_no_risk_state_row_logs_and_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If no current risk_state row exists, the hook logs a warning
        + returns without crashing."""
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.reconciliation.apply import ReconciliationKillSwitchContext

        def factory() -> Any:
            session = MagicMock()

            async def execute(stmt: Any, params: Any = None) -> Any:
                result = MagicMock()
                result.fetchone = MagicMock(return_value=None)
                return result

            session.execute = execute  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        hook = api_main._build_state_transition_hook(_settings())
        assert hook is not None

        ctx = ReconciliationKillSwitchContext(
            account_id=uuid4(),
            env="paper",
            actionable_break_count=1,
            primary_audit_event_uuid=uuid4(),
        )
        # Should NOT raise.
        await hook(ctx)

    @pytest.mark.asyncio
    async def test_normal_state_invokes_full_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NORMAL → plan_invoke_kill_switch + apply_state_transition + SSE emit."""
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.reconciliation.apply import ReconciliationKillSwitchContext

        def factory() -> Any:
            session = MagicMock()

            async def execute(stmt: Any, params: Any = None) -> Any:
                from types import SimpleNamespace as _NS

                result = MagicMock()
                result.fetchone = MagicMock(
                    return_value=_NS(
                        state="NORMAL",
                        severity=None,
                        convalescent_session_count=0,
                    )
                )
                return result

            session.execute = execute  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        # Mock plan_invoke_kill_switch to return a sentinel plan.
        sentinel_plan = MagicMock(reason="recon_mismatch")
        plan_calls: list[Any] = []

        def fake_plan(*args: Any, **kwargs: Any) -> Any:
            plan_calls.append(kwargs)
            return sentinel_plan

        from services.risk import state_machine as sm

        monkeypatch.setattr(sm, "plan_invoke_kill_switch", fake_plan)

        # Mock apply_state_transition.
        applied_audit_uuid = uuid4()
        applied_calls: list[Any] = []

        async def fake_apply(*, plan: Any, db: Any, account_id: Any, env: Any, phase_at_emit: Any):
            applied_calls.append(
                {"plan": plan, "account_id": account_id, "env": env, "phase": phase_at_emit}
            )
            return MagicMock(
                state_transition_audit_event_uuid=applied_audit_uuid,
                new_state="HALT_NEW",
                new_severity="routine",
            )

        from services.risk import dispatch as risk_dispatch

        monkeypatch.setattr(risk_dispatch, "apply_state_transition", fake_apply)

        # Mock SSE emit.
        sse_calls: list[Any] = []

        async def fake_emit(event_type: str, data: dict[str, Any]) -> int:
            sse_calls.append((event_type, data))
            return 99

        monkeypatch.setattr(api_main.sse_multiplexer, "emit_sse", fake_emit)

        hook = api_main._build_state_transition_hook(_settings())
        assert hook is not None

        triggering_uuid = uuid4()
        ctx = ReconciliationKillSwitchContext(
            account_id=uuid4(),
            env="paper",
            actionable_break_count=2,
            primary_audit_event_uuid=triggering_uuid,
        )
        await hook(ctx)

        # plan_invoke_kill_switch called with RECON_MISMATCH trigger.
        assert len(plan_calls) == 1
        from services.risk.state_machine import TransitionTrigger

        assert plan_calls[0]["trigger"] == TransitionTrigger.RECON_MISMATCH
        # apply_state_transition called.
        assert len(applied_calls) == 1
        # SSE emit happened.
        assert len(sse_calls) == 1
        assert sse_calls[0][0] == "risk_state"
        sse_data = sse_calls[0][1]
        assert sse_data["state"] == "HALT_NEW"
        assert sse_data["severity"] == "routine"
        assert sse_data["audit_event_uuid"] == str(applied_audit_uuid)
        assert sse_data["triggered_by"] == "auto_halt_recon_mismatch"
        assert sse_data["triggering_audit_event_uuid"] == str(triggering_uuid)

    @pytest.mark.asyncio
    async def test_apply_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An apply_state_transition exception should NOT propagate —
        the audit chain + alerts have already landed; the scheduler
        should continue ticking. Operator sees the error in logs."""
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.reconciliation.apply import ReconciliationKillSwitchContext

        def factory() -> Any:
            session = MagicMock()

            async def execute(stmt: Any, params: Any = None) -> Any:
                from types import SimpleNamespace as _NS

                result = MagicMock()
                result.fetchone = MagicMock(
                    return_value=_NS(
                        state="NORMAL",
                        severity=None,
                        convalescent_session_count=0,
                    )
                )
                return result

            session.execute = execute  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        from services.risk import dispatch as risk_dispatch
        from services.risk import state_machine as sm

        monkeypatch.setattr(sm, "plan_invoke_kill_switch", lambda **k: MagicMock(reason="x"))

        async def boom(**kwargs: Any) -> None:
            raise RuntimeError("DB unreachable")

        monkeypatch.setattr(risk_dispatch, "apply_state_transition", boom)

        hook = api_main._build_state_transition_hook(_settings())
        assert hook is not None
        # Must not raise.
        await hook(
            ReconciliationKillSwitchContext(
                account_id=uuid4(),
                env="paper",
                actionable_break_count=1,
                primary_audit_event_uuid=uuid4(),
            )
        )


class TestEntrypointAlertDispatchMapping:
    """sops yaml → API_DISCORD_WEBHOOK_URL_* + API_RESEND_* mappings."""

    def test_full_alert_dispatch_credentials_mapped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "discord:\n"
            "  webhook_urls:\n"
            "    alerts: https://discord.com/api/webhooks/A\n"
            "    critical: https://discord.com/api/webhooks/C\n"
            "resend:\n"
            "  api_key: re_yaml_test\n"
            "  from_address: ops@example.com\n"
            "  to_address: ops@example.com\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        for key in (
            "API_DISCORD_WEBHOOK_URL_ALERTS",
            "API_DISCORD_WEBHOOK_URL_CRITICAL",
            "API_RESEND_API_KEY",
            "API_RESEND_FROM_ADDRESS",
            "API_RESEND_TO_ADDRESS",
            "API_DATABASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            return None

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        import os

        assert os.environ["API_DISCORD_WEBHOOK_URL_ALERTS"] == "https://discord.com/api/webhooks/A"
        assert (
            os.environ["API_DISCORD_WEBHOOK_URL_CRITICAL"] == "https://discord.com/api/webhooks/C"
        )
        assert os.environ["API_RESEND_API_KEY"] == "re_yaml_test"
        assert os.environ["API_RESEND_FROM_ADDRESS"] == "ops@example.com"
        assert os.environ["API_RESEND_TO_ADDRESS"] == "ops@example.com"

    def test_partial_alert_dispatch_credentials_partial_promotion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Only the #alerts URL populated; #critical + Resend absent →
        # only API_DISCORD_WEBHOOK_URL_ALERTS lands in env. Hook will
        # construct without #critical + email but the closure is still
        # callable (P2 alerts succeed; P0 alerts will trip dispatcher).
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "discord:\n"
            "  webhook_urls:\n"
            "    alerts: https://discord.com/api/webhooks/A\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        for key in (
            "API_DISCORD_WEBHOOK_URL_ALERTS",
            "API_DISCORD_WEBHOOK_URL_CRITICAL",
            "API_RESEND_API_KEY",
            "API_RESEND_FROM_ADDRESS",
            "API_RESEND_TO_ADDRESS",
            "API_DATABASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            return None

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        import os

        assert os.environ["API_DISCORD_WEBHOOK_URL_ALERTS"] == "https://discord.com/api/webhooks/A"
        assert "API_DISCORD_WEBHOOK_URL_CRITICAL" not in os.environ
        assert "API_RESEND_API_KEY" not in os.environ

    def test_placeholder_url_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "discord:\n"
            "  webhook_urls:\n"
            "    alerts: <TODO_DISCORD_ALERTS_URL>\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        for key in (
            "API_DISCORD_WEBHOOK_URL_ALERTS",
            "API_DATABASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            return None

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        import os

        assert "API_DISCORD_WEBHOOK_URL_ALERTS" not in os.environ

    def test_missing_webhook_urls_block_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # `discord:` present but `webhook_urls:` missing → no env vars set.
        # Locks the `(secrets.get("discord") or {}).get("webhook_urls") or {}`
        # defensive chain.
        from services.api import entrypoint

        yaml_text = "postgres:\n  app_service_password: hexpwd\ndiscord:\n  api_bearer_token: xyz\n"
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        for key in (
            "API_DISCORD_WEBHOOK_URL_ALERTS",
            "API_DISCORD_WEBHOOK_URL_CRITICAL",
            "API_DATABASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            return None

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        # Should NOT raise.
        entrypoint.main(["true"])
        import os

        assert "API_DISCORD_WEBHOOK_URL_ALERTS" not in os.environ
        assert "API_DISCORD_WEBHOOK_URL_CRITICAL" not in os.environ


# ---------------------------------------------------------------------------
# PR E — float-secret guard (closes PR #138 follow-up #3)
# ---------------------------------------------------------------------------


class TestFindFloatSecretPaths:
    """``_find_float_secret_paths`` walks the sops dict tree + returns dotted
    paths where leaf values are Python floats.

    Catches the YAML numeric-coercion gotcha from Docs/decisions-log.md
    2026-05-12 (late) — IBKR's 24-digit FlexQuery token, unquoted in sops,
    round-trips through sops's float-aware writer as scientific notation,
    next read returns Python float instead of str.
    """

    def test_empty_dict_returns_empty(self) -> None:
        from services.api.entrypoint import _find_float_secret_paths

        assert _find_float_secret_paths({}) == []

    def test_top_level_float_detected(self) -> None:
        from services.api.entrypoint import _find_float_secret_paths

        assert _find_float_secret_paths({"flex_token": 1.527e23}) == ["flex_token"]

    def test_nested_float_detected_with_dotted_path(self) -> None:
        from services.api.entrypoint import _find_float_secret_paths

        secrets = {"ibkr": {"flex_query_token": 1.527e23, "flex_query_id": 1505530}}
        assert _find_float_secret_paths(secrets) == ["ibkr.flex_query_token"]

    def test_int_not_detected(self) -> None:
        # ibkr.flex_query_id is legitimately an int per sops yaml; must
        # NOT trip the guard.
        from services.api.entrypoint import _find_float_secret_paths

        assert _find_float_secret_paths({"ibkr": {"flex_query_id": 1505530}}) == []

    def test_str_not_detected(self) -> None:
        # Strings (passwords, tokens, etc.) are the expected type for
        # secret leaves; must NOT trip the guard.
        from services.api.entrypoint import _find_float_secret_paths

        secrets = {
            "postgres": {"app_service_password": "hexpwd"},
            "ibkr": {"flex_query_token": "152748490360752094531342"},
            "discord": {"api_bearer_token": "abc-def"},
        }
        assert _find_float_secret_paths(secrets) == []

    def test_bool_and_none_not_detected(self) -> None:
        # YAML booleans + nulls don't coerce to float; harmless.
        from services.api.entrypoint import _find_float_secret_paths

        secrets = {"flag": True, "missing": None, "other_flag": False}
        assert _find_float_secret_paths(secrets) == []

    def test_multiple_floats_returned(self) -> None:
        from services.api.entrypoint import _find_float_secret_paths

        secrets = {
            "ibkr": {"flex_query_token": 1.5e23},
            "discord": {"api_bearer_token": 2.5e23},
        }
        result = _find_float_secret_paths(secrets)
        assert set(result) == {"ibkr.flex_query_token", "discord.api_bearer_token"}

    def test_list_walked_recursively(self) -> None:
        from services.api.entrypoint import _find_float_secret_paths

        secrets = {"webhook_urls": ["url-1", 1.5e23, "url-2"]}
        assert _find_float_secret_paths(secrets) == ["webhook_urls.[1]"]

    def test_deeply_nested_float_detected(self) -> None:
        from services.api.entrypoint import _find_float_secret_paths

        secrets = {"a": {"b": {"c": {"d": 1.5e23}}}}
        assert _find_float_secret_paths(secrets) == ["a.b.c.d"]

    def test_root_float_returns_root_label(self) -> None:
        from services.api.entrypoint import _find_float_secret_paths

        assert _find_float_secret_paths(1.5e23) == ["<root>"]


class TestEntrypointFloatSecretGuard:
    """``main()`` exits with the operator-readable hint when a float secret
    is detected. End-to-end wiring of the guard.
    """

    def test_float_token_exits_with_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from services.api import entrypoint

        # Simulate sops yaml that has been mangled by YAML numeric coercion.
        # PyYAML safe_load sees `1.527484903607521e+23` as float, NOT str.
        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  flex_query_token: 1.527484903607521e+23\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        exit_code = entrypoint.main(["true"])
        assert exit_code == 2
        captured = capsys.readouterr()
        # Operator-readable hint surfaces the path + the fix + paper trail.
        assert "ibkr.flex_query_token" in captured.err
        assert "double quotes" in captured.err
        assert "decisions-log" in captured.err
        # Did NOT proceed to exec uvicorn.
        assert "cmd" not in called

    def test_well_quoted_string_token_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from services.api import entrypoint

        # Same value but quoted → loaded as str → passes the guard.
        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            '  flex_query_token: "152748490360752094531342"\n'
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.delenv("API_FLEX_QUERY_TOKEN", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        assert "cmd" in called
        import os

        assert os.environ["API_FLEX_QUERY_TOKEN"] == "152748490360752094531342"

    def test_int_id_alongside_quoted_token_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Regression case: ibkr.flex_query_id is legitimately an int +
        # ibkr.flex_query_token is a quoted string. Neither trips the
        # guard. The canonical sops shape post-PR-#137 fix.
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  flex_query_id: 1505530\n"
            '  flex_query_token: "152748490360752094531342"\n'
            "  paper_account: DUQ_TEST\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        for key in (
            "API_DATABASE_URL",
            "API_FLEX_QUERY_ID",
            "API_FLEX_QUERY_TOKEN",
            "API_IBKR_ACCOUNT",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        called: dict[str, Any] = {}

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            called["cmd"] = cmd

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        entrypoint.main(["true"])
        assert "cmd" in called

    def test_multiple_floats_all_surface_in_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # If two secrets got float-coerced, both paths surface in the
        # error so the operator fixes them in one sops edit.
        from services.api import entrypoint

        yaml_text = (
            "postgres:\n"
            "  app_service_password: hexpwd\n"
            "ibkr:\n"
            "  flex_query_token: 1.527484903607521e+23\n"
            "discord:\n"
            "  api_bearer_token: 9.876543210987654e+22\n"
        )
        secrets_path = tmp_path / "decrypted.yaml"
        secrets_path.write_text(yaml_text)
        monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            return None

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        exit_code = entrypoint.main(["true"])
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "ibkr.flex_query_token" in captured.err
        assert "discord.api_bearer_token" in captured.err

    def test_missing_secrets_file_no_false_positive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # When the sops bundle doesn't exist (dev mode), the guard sees
        # an empty dict + does not trip. The downstream pg_password
        # check fires instead with the canonical placeholder exit.
        from services.api import entrypoint

        monkeypatch.setenv("API_SECRETS_PATH", str(tmp_path / "nonexistent.yaml"))
        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        monkeypatch.setenv("API_ENVIRONMENT", "paper")

        def fake_execvp(cmd: str, argv: list[str]) -> None:
            return None

        monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
        exit_code = entrypoint.main(["true"])
        # exit 2 from missing postgres password, NOT from the float guard.
        assert exit_code == 2
