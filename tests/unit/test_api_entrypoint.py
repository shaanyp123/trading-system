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
