"""Unit tests for ``scripts/operator_tools/bootstrap_live_account.py``.

A22 N/A: the script's DB-touching code paths require a real Postgres
testcontainer to exercise meaningfully; unit tests cover the argparse +
JSON-validation surface + the dry-run + bad-args + bad-JSON exit paths.

Coverage matrix:

* Argparse happy path with all required + defaulted args.
* --env live-small WITHOUT --allow-non-paper rejected.
* --no-dry-run WITHOUT --confirm rejected.
* --no-dry-run AND --confirm with --env paper accepted.
* load_parameter_set_json — happy path returns ParameterSetPayload.
* load_parameter_set_json — missing keys / wrong types / bad hash length
  rejected with ValueError.
* main() with --dry-run completes with EXIT_OK + no DB connection.
* main() with bad parameter-set JSON returns EXIT_BAD_PARAM_SET_JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.operator_tools.bootstrap_live_account import (
    DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID,
    EXIT_BAD_ARGS,
    EXIT_BAD_PARAM_SET_JSON,
    EXIT_OK,
    ParameterSetPayload,
    ParsedArgs,
    load_parameter_set_json,
    main,
    parse_args,
)


class TestParseArgs:
    def test_paper_dry_run_defaults(self) -> None:
        result = parse_args(["--env", "paper"])
        assert isinstance(result, ParsedArgs)
        assert result.external_account_id == DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID
        assert result.env == "paper"
        assert result.dry_run is True
        assert result.confirm is False
        assert result.allow_non_paper is False
        assert result.parameter_set_json is None

    def test_live_small_without_allow_non_paper_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(["--env", "live-small"])

    def test_live_scale_without_allow_non_paper_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(["--env", "live-scale"])

    def test_live_small_with_allow_non_paper_accepted(self) -> None:
        result = parse_args(["--env", "live-small", "--allow-non-paper"])
        assert result.env == "live-small"
        assert result.allow_non_paper is True

    def test_no_dry_run_without_confirm_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --confirm"):
            parse_args(["--env", "paper", "--no-dry-run"])

    def test_no_dry_run_with_confirm_accepted(self) -> None:
        result = parse_args(["--env", "paper", "--no-dry-run", "--confirm"])
        assert result.dry_run is False
        assert result.confirm is True

    def test_external_account_id_override(self) -> None:
        result = parse_args(
            [
                "--env",
                "live-small",
                "--allow-non-paper",
                "--external-account-id",
                "U00000000",
            ]
        )
        assert result.external_account_id == "U00000000"

    def test_parameter_set_json_path_propagates(self, tmp_path: Path) -> None:
        path = tmp_path / "ps.json"
        path.write_text('{"x": 1}')
        result = parse_args(["--env", "paper", "--parameter-set-json", str(path)])
        assert result.parameter_set_json == path


class TestLoadParameterSetJson:
    def _write(self, tmp_path: Path, content: object) -> Path:
        path = tmp_path / "ps.json"
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_text(json.dumps(content))
        return path

    def test_happy_path(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            {"parameter_set_hash": "a" * 64, "parameters": {"X": 0.15}},
        )
        result = load_parameter_set_json(path)
        assert isinstance(result, ParameterSetPayload)
        assert result.parameter_set_hash == "a" * 64
        assert result.parameters == {"X": 0.15}

    def test_top_level_not_object_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, ["not", "an", "object"])
        with pytest.raises(ValueError, match="top-level must be a JSON object"):
            load_parameter_set_json(path)

    def test_missing_hash_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameters": {"X": 1}})
        with pytest.raises(ValueError, match="missing 'parameter_set_hash'"):
            load_parameter_set_json(path)

    def test_missing_parameters_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": "a" * 64})
        with pytest.raises(ValueError, match="missing 'parameters'"):
            load_parameter_set_json(path)

    def test_hash_wrong_length_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": "abc", "parameters": {}})
        with pytest.raises(ValueError, match="must be 64 hex chars"):
            load_parameter_set_json(path)

    def test_hash_wrong_type_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": 12345, "parameters": {}})
        with pytest.raises(ValueError, match="'parameter_set_hash' must be a string"):
            load_parameter_set_json(path)

    def test_parameters_wrong_type_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": "a" * 64, "parameters": "not-a-dict"})
        with pytest.raises(ValueError, match="'parameters' must be a JSON object"):
            load_parameter_set_json(path)

    def test_malformed_json_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "ps.json"
        path.write_text("not valid json {{{")
        with pytest.raises(ValueError, match="failed to read/parse"):
            load_parameter_set_json(path)

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        with pytest.raises(ValueError, match="failed to read/parse"):
            load_parameter_set_json(path)


class TestMainExits:
    def test_dry_run_returns_ok_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run default returns 0 + no DB connection attempted.

        Sentinel: DATABASE_URL env unset → if the code reached the DB
        init path it would return EXIT_DB_INIT_FAILED (=5). EXIT_OK
        proves the dry-run short-circuit fires before DB init.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        result = main(["--env", "paper"])
        assert result == EXIT_OK

    def test_bad_param_set_json_returns_exit_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed parameter-set JSON returns EXIT_BAD_PARAM_SET_JSON."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        path = tmp_path / "ps.json"
        path.write_text("not valid json {{{")
        result = main(
            [
                "--env",
                "paper",
                "--parameter-set-json",
                str(path),
            ]
        )
        assert result == EXIT_BAD_PARAM_SET_JSON

    def test_invalid_args_returns_exit_6(self) -> None:
        """--env live-small WITHOUT --allow-non-paper returns EXIT_BAD_ARGS."""
        result = main(["--env", "live-small"])
        assert result == EXIT_BAD_ARGS
