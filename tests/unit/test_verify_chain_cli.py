"""Unit tests for ``services.audit.verify_chain.main`` — the operator CLI.

A22 binds: these tests exercise the CLI's argparse + env-var + stdout
contract without touching Postgres. The DB-bound walker (``verify_chain``
itself) has its row-count contract verified in
``tests/integration/test_audit_writer.py`` against testcontainers.

Coverage goal: every branch in ``main()`` plus the env-validation logic.
``services/audit/**`` requires 90% per dev-guide §6.7 — these tests
cover the CLI module fully so the directory aggregate stays above the
floor.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.audit import verify_chain as cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_verify_returning(ok: bool, broken: int | None, walked: int) -> Any:
    """Build an awaitable stub for monkeypatching :func:`cli._verify`.

    The CLI calls ``await _verify(database_url)`` — so the stub must be
    an async function returning the canned tuple regardless of argument.
    """

    async def _stub(_database_url: str) -> tuple[bool, int | None, int]:
        return ok, broken, walked

    return _stub


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestArgparse:
    def test_env_required(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main([])
        # argparse exits with 2 on missing required arg.
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--env" in err

    def test_env_rejects_invalid_value(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--env", "production"])
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "invalid choice" in err
        # The four allowed environments must show in the error message
        # (argparse renders them automatically).
        for env in ("dev", "paper", "live-small", "live-scale"):
            assert env in err

    @pytest.mark.parametrize(
        "env",
        ["dev", "paper", "live-small", "live-scale"],
    )
    def test_env_accepts_each_allowed_value(
        self,
        env: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(cli.DATABASE_URL_ENV, "postgresql+asyncpg://stub/db")
        monkeypatch.setattr(cli, "_verify", _stub_verify_returning(True, None, 0))
        rc = cli.main(["--env", env])
        assert rc == cli.EXIT_OK
        # Sanity: the success line landed on stdout.
        assert "CHAIN OK" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# DATABASE_URL handling
# ---------------------------------------------------------------------------


class TestDatabaseUrlEnv:
    def test_missing_env_var_returns_exit_usage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv(cli.DATABASE_URL_ENV, raising=False)
        rc = cli.main(["--env", "paper"])
        assert rc == cli.EXIT_USAGE
        err = capsys.readouterr().err
        assert cli.DATABASE_URL_ENV in err
        # Runbook pointer is load-bearing per the module docstring contract.
        assert "deploy/audit/README.md" in err

    def test_empty_env_var_treated_as_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Operator might `export DATABASE_URL=` accidentally; the bash
        # convention is that an empty value is functionally absent. Our
        # CLI must treat it the same as fully-missing or the eventual
        # asyncpg connection error would be more confusing than the
        # canonical "set the env var" message.
        monkeypatch.setenv(cli.DATABASE_URL_ENV, "")
        rc = cli.main(["--env", "paper"])
        assert rc == cli.EXIT_USAGE
        assert cli.DATABASE_URL_ENV in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Mainline output: clean walk, broken chain, empty table
# ---------------------------------------------------------------------------


class TestCleanWalk:
    def test_three_row_clean_walk_prints_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(cli.DATABASE_URL_ENV, "postgresql+asyncpg://stub/db")
        monkeypatch.setattr(cli, "_verify", _stub_verify_returning(True, None, 3))
        rc = cli.main(["--env", "paper"])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        # Operator-runbook contract: exact substring match on the success line.
        assert "CHAIN OK: 3 rows verified" in out

    def test_empty_table_prints_zero_rows(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(cli.DATABASE_URL_ENV, "postgresql+asyncpg://stub/db")
        monkeypatch.setattr(cli, "_verify", _stub_verify_returning(True, None, 0))
        rc = cli.main(["--env", "dev"])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "CHAIN OK: 0 rows verified" in out

    def test_large_chain_count_prints_unformatted_int(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # No thousands separator — operators grep for "CHAIN OK: <int> rows
        # verified", not "CHAIN OK: 1,234,567 rows verified".
        monkeypatch.setenv(cli.DATABASE_URL_ENV, "postgresql+asyncpg://stub/db")
        monkeypatch.setattr(cli, "_verify", _stub_verify_returning(True, None, 1_234_567))
        rc = cli.main(["--env", "live-small"])
        assert rc == cli.EXIT_OK
        assert "CHAIN OK: 1234567 rows verified" in capsys.readouterr().out


class TestChainBreak:
    def test_break_at_seq_2_after_1_verified_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(cli.DATABASE_URL_ENV, "postgresql+asyncpg://stub/db")
        monkeypatch.setattr(cli, "_verify", _stub_verify_returning(False, 2, 1))
        rc = cli.main(["--env", "paper"])
        # Non-zero exit is the load-bearing signal for the runbook.
        assert rc == cli.EXIT_CHAIN_BREAK
        out = capsys.readouterr().out
        assert "CHAIN BREAK at sequence_no=2" in out
        # The "(after K verified rows)" suffix tells the operator which
        # rows are vouched-for vs. which need re-export from the canonical
        # source (the QC adapter + paper trading audit upstream).
        assert "after 1 verified rows" in out

    def test_break_at_seq_1_genesis_violation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # If sequence_no=1 itself has a non-genesis prev_hash, the walker
        # returns (False, 1, 0) — nothing was verified before the break.
        monkeypatch.setenv(cli.DATABASE_URL_ENV, "postgresql+asyncpg://stub/db")
        monkeypatch.setattr(cli, "_verify", _stub_verify_returning(False, 1, 0))
        rc = cli.main(["--env", "paper"])
        assert rc == cli.EXIT_CHAIN_BREAK
        out = capsys.readouterr().out
        assert "CHAIN BREAK at sequence_no=1" in out
        assert "after 0 verified rows" in out


# ---------------------------------------------------------------------------
# Module-level constants (lock the CLI contract)
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_exit_codes_locked(self) -> None:
        # These are the exit-code semantics the runbook documents and any
        # automated wrapper (cron, CI smoke) keys off. Changing them is a
        # spec-deviation that needs a forbidden-whitelist PR with the
        # runbook updated in the same commit.
        assert cli.EXIT_OK == 0
        assert cli.EXIT_CHAIN_BREAK == 1
        assert cli.EXIT_USAGE == 2

    def test_database_url_env_name_is_bare_database_url(self) -> None:
        # We deliberately use the conventional ``DATABASE_URL`` rather than
        # ``API_DATABASE_URL`` (the api container's prefixed name) — the
        # CLI is invoked outside the api process and its env-var contract
        # is independent.
        assert cli.DATABASE_URL_ENV == "DATABASE_URL"

    def test_main_is_exported(self) -> None:
        assert "main" in cli.__all__
