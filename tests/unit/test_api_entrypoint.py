"""Unit tests for `services/api/entrypoint.py` (sops yaml → env vars).

The real entrypoint exec()s uvicorn, so tests inject a stub argv that
echoes its environment back via `python -c` and assert against stdout. We
only call entrypoint logic up to the exec by passing a no-op command.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from services.api import entrypoint


def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "decrypted.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def test_build_database_url_uses_default_host_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_DB_HOST", raising=False)
    monkeypatch.delenv("API_DB_NAME", raising=False)
    monkeypatch.delenv("API_DB_USER", raising=False)
    monkeypatch.delenv("API_DB_PORT", raising=False)
    url = entrypoint._build_database_url("hexpwd123")
    assert url == "postgresql+asyncpg://app_service:hexpwd123@postgres:5432/trading"


def test_build_database_url_honors_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_DB_HOST", "pg.internal")
    monkeypatch.setenv("API_DB_PORT", "5433")
    monkeypatch.setenv("API_DB_NAME", "tradedb")
    monkeypatch.setenv("API_DB_USER", "custom_role")
    url = entrypoint._build_database_url("pwd")
    assert url == "postgresql+asyncpg://custom_role:pwd@pg.internal:5433/tradedb"


def test_looks_like_placeholder_catches_todo_markers() -> None:
    assert entrypoint._looks_like_placeholder("<TODO_FROM_DAY_3_POSTGRES_BOOTSTRAP>")
    assert entrypoint._looks_like_placeholder("null")
    assert entrypoint._looks_like_placeholder(None)
    assert entrypoint._looks_like_placeholder("")
    assert not entrypoint._looks_like_placeholder("realhexpassword")


def test_load_secrets_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert entrypoint._load_secrets(tmp_path / "nope.yaml") == {}


def test_load_secrets_parses_yaml(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: hex123
        internal:
          watchdog_bearer_token: bearer456
        """,
    )
    parsed = entrypoint._load_secrets(path)
    assert parsed["postgres"]["app_service_password"] == "hex123"
    assert parsed["internal"]["watchdog_bearer_token"] == "bearer456"


def test_main_exits_2_on_placeholder_postgres_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets_path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: <TODO_FROM_DAY_3_POSTGRES_BOOTSTRAP>
        """,
    )
    monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)

    rc = entrypoint.main(argv=["true"])  # argv ignored on the early-exit path
    assert rc == 2
    captured = capsys.readouterr()
    assert "placeholder" in captured.err.lower() or "<TODO" in captured.err


def test_main_exits_2_when_no_secrets_and_no_database_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("API_SECRETS_PATH", str(tmp_path / "no-such-file.yaml"))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)

    rc = entrypoint.main(argv=["true"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "placeholder" in captured.err.lower()


def test_main_uses_existing_database_url_without_secrets_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If API_DATABASE_URL is already set in the environment, the entrypoint
    skips sops parsing entirely and exec()s the supplied argv. We replace
    `os.execvp` with a stub so the test process survives."""
    monkeypatch.setenv("API_SECRETS_PATH", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv(
        "API_DATABASE_URL",
        "postgresql+asyncpg://app_service:test@postgres:5432/trading",
    )

    captured: dict[str, Any] = {}

    def fake_exec(cmd: str, args: list[str]) -> None:
        captured["cmd"] = cmd
        captured["args"] = args

    monkeypatch.setattr(os, "execvp", fake_exec)

    entrypoint.main(argv=["true", "--noop"])

    assert captured["cmd"] == "true"
    assert captured["args"] == ["true", "--noop"]
    assert os.environ["API_VERSION"]  # set by the entrypoint side-effect


def test_main_extracts_watchdog_bearer_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: realhex123
        internal:
          watchdog_bearer_token: bearer-real-value
        """,
    )
    monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    monkeypatch.delenv("API_WATCHDOG_BEARER_TOKEN", raising=False)

    captured: dict[str, Any] = {}

    def fake_exec(cmd: str, args: list[str]) -> None:
        captured["cmd"] = cmd
        captured["env_db"] = os.environ.get("API_DATABASE_URL")
        captured["env_watchdog"] = os.environ.get("API_WATCHDOG_BEARER_TOKEN")

    monkeypatch.setattr(os, "execvp", fake_exec)

    entrypoint.main(argv=["true"])

    assert captured["env_db"] == (
        "postgresql+asyncpg://app_service:realhex123@postgres:5432/trading"
    )
    assert captured["env_watchdog"] == "bearer-real-value"


def test_main_extracts_discord_bot_bearer_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Day 23: sops `discord.api_bearer_token` → `API_DISCORD_BOT_BEARER_TOKEN`."""
    secrets_path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: realhex123
        discord:
          api_bearer_token: bot-bearer-secret
        """,
    )
    monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    monkeypatch.delenv("API_DISCORD_BOT_BEARER_TOKEN", raising=False)

    captured: dict[str, str | None] = {}

    def fake_exec(cmd: str, args: list[str]) -> None:
        captured["env_bot"] = os.environ.get("API_DISCORD_BOT_BEARER_TOKEN")

    monkeypatch.setattr(os, "execvp", fake_exec)

    entrypoint.main(argv=["true"])

    assert captured["env_bot"] == "bot-bearer-secret"


def test_main_extracts_totp_encryption_key_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Day 21 carryover: sops `totp.encryption_key` → `API_TOTP_ENCRYPTION_KEY`."""
    secrets_path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: realhex123
        totp:
          encryption_key: base64url-encoded-32-byte-key-placeholder
        """,
    )
    monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    monkeypatch.delenv("API_TOTP_ENCRYPTION_KEY", raising=False)

    captured: dict[str, str | None] = {}

    def fake_exec(cmd: str, args: list[str]) -> None:
        captured["env_totp"] = os.environ.get("API_TOTP_ENCRYPTION_KEY")

    monkeypatch.setattr(os, "execvp", fake_exec)

    entrypoint.main(argv=["true"])

    assert captured["env_totp"] == "base64url-encoded-32-byte-key-placeholder"


def test_main_extracts_webauthn_triplet_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Day 21 carryover: sops `webauthn.{rp_id,rp_name,origin}` → 3 env vars."""
    secrets_path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: realhex123
        webauthn:
          rp_id: spratcapital.com
          rp_name: trading-system
          origin: https://spratcapital.com
        """,
    )
    monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    monkeypatch.delenv("API_WEBAUTHN_RP_ID", raising=False)
    monkeypatch.delenv("API_WEBAUTHN_RP_NAME", raising=False)
    monkeypatch.delenv("API_WEBAUTHN_ORIGIN", raising=False)

    captured: dict[str, str | None] = {}

    def fake_exec(cmd: str, args: list[str]) -> None:
        captured["rp_id"] = os.environ.get("API_WEBAUTHN_RP_ID")
        captured["rp_name"] = os.environ.get("API_WEBAUTHN_RP_NAME")
        captured["origin"] = os.environ.get("API_WEBAUTHN_ORIGIN")

    monkeypatch.setattr(os, "execvp", fake_exec)

    entrypoint.main(argv=["true"])

    assert captured["rp_id"] == "spratcapital.com"
    assert captured["rp_name"] == "trading-system"
    assert captured["origin"] == "https://spratcapital.com"


def test_main_does_not_override_preset_day21_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Day-21 env vars are preset, sops values do NOT clobber them.

    Same defensive pattern as `API_WATCHDOG_BEARER_TOKEN` — operator can
    override via docker-compose `environment:` block without round-tripping
    through sops.
    """
    secrets_path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: realhex123
        totp:
          encryption_key: from-sops
        webauthn:
          rp_id: from-sops.example
          rp_name: from-sops-name
          origin: https://from-sops.example
        discord:
          api_bearer_token: from-sops-bot
        """,
    )
    monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    monkeypatch.setenv("API_TOTP_ENCRYPTION_KEY", "preset-totp")
    monkeypatch.setenv("API_WEBAUTHN_RP_ID", "preset.example")
    monkeypatch.setenv("API_WEBAUTHN_RP_NAME", "preset-name")
    monkeypatch.setenv("API_WEBAUTHN_ORIGIN", "https://preset.example")
    monkeypatch.setenv("API_DISCORD_BOT_BEARER_TOKEN", "preset-bot")

    captured: dict[str, str | None] = {}

    def fake_exec(cmd: str, args: list[str]) -> None:
        captured["totp"] = os.environ.get("API_TOTP_ENCRYPTION_KEY")
        captured["rp_id"] = os.environ.get("API_WEBAUTHN_RP_ID")
        captured["rp_name"] = os.environ.get("API_WEBAUTHN_RP_NAME")
        captured["origin"] = os.environ.get("API_WEBAUTHN_ORIGIN")
        captured["bot"] = os.environ.get("API_DISCORD_BOT_BEARER_TOKEN")

    monkeypatch.setattr(os, "execvp", fake_exec)

    entrypoint.main(argv=["true"])

    assert captured["totp"] == "preset-totp"
    assert captured["rp_id"] == "preset.example"
    assert captured["rp_name"] == "preset-name"
    assert captured["origin"] == "https://preset.example"
    assert captured["bot"] == "preset-bot"


def test_main_skips_day21_env_vars_when_sops_key_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Placeholder sops values must NOT land in env (downstream code
    catches the missing-key case at runtime; degrading gracefully here
    is consistent with the watchdog + bot bearer mapping pattern)."""
    secrets_path = _write_yaml(
        tmp_path,
        """
        postgres:
          app_service_password: realhex123
        totp:
          encryption_key: <TODO_FROM_DAY_24_CEREMONY>
        webauthn:
          rp_id: <TODO_FROM_DAY_24_CEREMONY>
          rp_name: trading-system
          origin: <TODO_FROM_DAY_24_CEREMONY>
        discord:
          api_bearer_token: <TODO_FROM_DAY_24_CEREMONY>
        """,
    )
    monkeypatch.setenv("API_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    for var in [
        "API_TOTP_ENCRYPTION_KEY",
        "API_WEBAUTHN_RP_ID",
        "API_WEBAUTHN_RP_NAME",
        "API_WEBAUTHN_ORIGIN",
        "API_DISCORD_BOT_BEARER_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)

    captured: dict[str, str | None] = {}

    def fake_exec(cmd: str, args: list[str]) -> None:
        captured["totp"] = os.environ.get("API_TOTP_ENCRYPTION_KEY")
        captured["rp_id"] = os.environ.get("API_WEBAUTHN_RP_ID")
        captured["rp_name"] = os.environ.get("API_WEBAUTHN_RP_NAME")
        captured["origin"] = os.environ.get("API_WEBAUTHN_ORIGIN")
        captured["bot"] = os.environ.get("API_DISCORD_BOT_BEARER_TOKEN")

    monkeypatch.setattr(os, "execvp", fake_exec)

    entrypoint.main(argv=["true"])

    # Placeholder values are NOT exported. The non-placeholder rp_name
    # IS exported (each key is individually checked).
    assert captured["totp"] is None
    assert captured["rp_id"] is None
    assert captured["rp_name"] == "trading-system"
    assert captured["origin"] is None
    assert captured["bot"] is None
