"""Unit tests for ``scripts/operator_tools/apply_parameter_change.py`` (PR-D).

A22 N/A — the DB-touching ceremony is exercised via a stub
``session_factory_override`` + a monkeypatched ``append_audit_event``; no live
Postgres. The high-value assertions are the **audit-first ordering** (the audit
row is emitted before any state-change write) and the **hash-stable
``prev == hash``** row shape (§13.3).
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts.operator_tools.apply_parameter_change import (
    EXIT_BAD_INPUT,
    EXIT_NO_ACCOUNT,
    EXIT_NO_HEAD_ROW,
    EXIT_OK,
    ParsedArgs,
    _amain,
    _build_parameter_change_payload,
    _changed_by_for,
    main,
    parse_args,
)

_HEX = set("0123456789abcdef")
_AUDIT_UUID = UUID("0190aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee")
_ACCOUNT_UUID = "0190ffff-1111-7222-8333-444444444444"


# ---------------------------------------------------------------------------
# Stub session + factory (records statement kind + params, shared recorder)
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _Txn:
    async def __aenter__(self) -> _Txn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Session:
    def __init__(
        self, head_row: object, account_row: object, recorder: list[tuple[str, object]]
    ) -> None:
        self._head = head_row
        self._account = account_row
        self._rec = recorder

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _Txn:
        return _Txn()

    async def execute(self, statement: object, params: object = None) -> _Result:
        sql = str(statement)
        if "SELECT parameter_set_hash" in sql:
            self._rec.append(("select_head", params))
            return _Result(self._head)
        if "FROM accounts" in sql:
            self._rec.append(("select_account", params))
            return _Result(self._account)
        if "UPDATE parameters SET valid_to" in sql:
            self._rec.append(("close_prior", params))
        elif "INSERT INTO parameters" in sql:
            self._rec.append(("insert_param", params))
        elif "UPDATE parameter_sets" in sql:
            self._rec.append(("update_head", params))
        return _Result(None)


def _make_factory(
    head_row: object, account_row: object, recorder: list[tuple[str, object]]
) -> Callable[[], _Session]:
    def factory() -> _Session:
        return _Session(head_row, account_row, recorder)

    return factory


def _make_fake_append(recorder: list[tuple[str, object]]) -> Callable[..., object]:
    async def _fake_append(
        session: object,
        event_type: object,
        payload: object,
        *,
        account_id: object,
        env: object,
        phase_at_emit: object,
        **kw: object,
    ) -> object:
        recorder.append(("audit", {"event_type": str(event_type), "payload": payload}))
        return SimpleNamespace(event_uuid=_AUDIT_UUID, sequence_no=42)

    return _fake_append


def _wet_args(*, value: str = "true") -> ParsedArgs:
    return parse_args(
        [
            "--env",
            "paper",
            "--parameter",
            "STRATEGY_DECOMMISSIONED",
            "--value",
            value,
            "--reason",
            "test",
            "--no-dry-run",
            "--confirm",
        ]
    )


# ---------------------------------------------------------------------------
# parse_args + pure helpers
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_dry_run_default(self) -> None:
        a = parse_args(
            ["--env", "paper", "--parameter", "STRATEGY_DECOMMISSIONED", "--value", "true"]
        )
        assert a.dry_run is True and a.confirm is False
        assert a.value is True and a.parameter == "STRATEGY_DECOMMISSIONED"

    def test_value_false_parses(self) -> None:
        a = parse_args(["--env", "paper", "--parameter", "EXIT_AUTO_APPROVE", "--value", "false"])
        assert a.value is False

    def test_bad_parameter_rejected(self) -> None:
        # A ranges param is not in the operator-flag allowlist → argparse rejects
        # it (exit != 0). Locks the scope boundary: ranges params can't be flipped
        # by this tool (they'd move the hash — out of §13.3 scope).
        assert (
            main(["--env", "paper", "--parameter", "LOOKBACK_DAYS_DONCHIAN", "--value", "true"])
            != EXIT_OK
        )

    def test_live_requires_allow_non_paper(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(
                ["--env", "live-small", "--parameter", "STRATEGY_DECOMMISSIONED", "--value", "true"]
            )

    def test_no_dry_run_requires_confirm(self) -> None:
        with pytest.raises(ValueError, match="requires --confirm"):
            parse_args(
                [
                    "--env",
                    "paper",
                    "--parameter",
                    "STRATEGY_DECOMMISSIONED",
                    "--value",
                    "true",
                    "--no-dry-run",
                ]
            )

    def test_changed_by_mapping(self) -> None:
        assert _changed_by_for(True) == "operator"
        assert _changed_by_for(False) == "revert"


class TestPayload:
    def test_prev_equals_hash_and_jcs_safe(self) -> None:
        p = _build_parameter_change_payload(
            parameter_name="STRATEGY_DECOMMISSIONED",
            old_value_str="False",
            new_value_str="True",
            parameter_set_hash="a" * 64,
            changed_by="operator",
            change_reason="r",
        )
        assert p["parameter_set_hash"] == p["prev_parameter_set_hash"] == "a" * 64
        assert p["old_value"] == "False" and p["new_value"] == "True"
        assert set("".join(str(p[k]) for k in ("parameter_set_hash",))) <= _HEX
        # JCS-safe: only str / None values here
        assert all(v is None or isinstance(v, str) for v in p.values())


class TestDryRun:
    def test_dry_run_no_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # DATABASE_URL unset → reaching the DB path would return EXIT_DB_INIT_FAILED.
        # EXIT_OK proves the dry-run short-circuit fires before any DB access.
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert main(
            ["--env", "paper", "--parameter", "STRATEGY_DECOMMISSIONED", "--value", "true"]
        ) == (EXIT_OK)


# ---------------------------------------------------------------------------
# Wet ceremony — audit-first ordering + prev==hash + guards
# ---------------------------------------------------------------------------


class TestWetCeremony:
    async def test_audit_first_ordering_and_prev_equals_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        head = _Row(parameter_set_hash="a" * 64, parameters={"STRATEGY_DECOMMISSIONED": "False"})
        account = _Row(id=_ACCOUNT_UUID)
        factory = _make_factory(head, account, recorder)

        rc = await _amain(_wet_args(value="true"), session_factory_override=factory)
        assert rc == EXIT_OK

        kinds = [k for k, _ in recorder]
        # The audit event is emitted BEFORE any state-change write (audit-first).
        assert "audit" in kinds
        audit_i = kinds.index("audit")
        assert (
            audit_i
            < kinds.index("close_prior")
            < kinds.index("insert_param")
            < kinds.index("update_head")
        )
        # The new parameters row is hash-stable: prev == hash; FK = the audit uuid.
        insert_params = next(p for k, p in recorder if k == "insert_param")
        assert isinstance(insert_params, dict)
        assert insert_params["hash"] == insert_params["prev_hash"] == "a" * 64
        assert insert_params["audit_uuid"] == _AUDIT_UUID
        assert insert_params["pval"] == "True"
        assert insert_params["changed_by"] == "operator"
        # The head UPDATE flips the same flag to the same canonical string.
        update_params = next(p for k, p in recorder if k == "update_head")
        assert isinstance(update_params, dict)
        assert update_params["pname"] == "STRATEGY_DECOMMISSIONED"
        assert update_params["pval"] == "True"

    async def test_revert_emits_reverted_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        head = _Row(parameter_set_hash="a" * 64, parameters={"STRATEGY_DECOMMISSIONED": "True"})
        factory = _make_factory(head, _Row(id=_ACCOUNT_UUID), recorder)

        rc = await _amain(_wet_args(value="false"), session_factory_override=factory)
        assert rc == EXIT_OK
        audit = next(p for k, p in recorder if k == "audit")
        assert isinstance(audit, dict)
        assert "parameter_change_reverted" in str(audit["event_type"])
        insert_params = next(p for k, p in recorder if k == "insert_param")
        assert isinstance(insert_params, dict)
        assert insert_params["changed_by"] == "revert"

    async def test_no_head_row_errors_before_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        factory = _make_factory(None, _Row(id=_ACCOUNT_UUID), recorder)
        rc = await _amain(_wet_args(value="true"), session_factory_override=factory)
        assert rc == EXIT_NO_HEAD_ROW
        assert "audit" not in [k for k, _ in recorder]  # never wrote audit

    async def test_noop_when_already_at_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        head = _Row(parameter_set_hash="a" * 64, parameters={"STRATEGY_DECOMMISSIONED": "True"})
        factory = _make_factory(head, _Row(id=_ACCOUNT_UUID), recorder)
        rc = await _amain(_wet_args(value="true"), session_factory_override=factory)
        assert rc == EXIT_OK
        kinds = [k for k, _ in recorder]
        assert "audit" not in kinds and "insert_param" not in kinds  # no-op: nothing written

    async def test_no_account_errors_before_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        head = _Row(parameter_set_hash="a" * 64, parameters={"STRATEGY_DECOMMISSIONED": "False"})
        factory = _make_factory(head, None, recorder)
        rc = await _amain(_wet_args(value="true"), session_factory_override=factory)
        assert rc == EXIT_NO_ACCOUNT
        assert "audit" not in [k for k, _ in recorder]


# Touch EXIT_BAD_INPUT so an unused-import lint can't hide a contract regression.
def test_bad_input_code_exposed() -> None:
    assert EXIT_BAD_INPUT == 1
