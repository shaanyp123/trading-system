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
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


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
