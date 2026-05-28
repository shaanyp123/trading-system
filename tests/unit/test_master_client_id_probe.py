"""Unit tests for ``scripts/operator_tools/master_client_id_probe.py``.

A22 N/A: the probe script does no DB writes; tests exercise the
argparse surface + validation rules + exit-code contract. The
async stage orchestration is exercised manually by the operator
against the live gateway per the runbook in the script's module
docstring.

Coverage matrix:

* Argparse happy path with all required + defaulted args.
* --place-client-id collision with --master-client-id rejected.
* --cleanup-client-id collision with --master-client-id rejected.
* --cleanup-client-id collision with --place-client-id rejected.
* Out-of-range --place-client-id rejected (must be 80-99 per
  dev-guide §1.5).
* Out-of-range --cleanup-client-id rejected.
* --env live-* without --allow-non-paper rejected.
* --env live-* with --allow-non-paper accepted.
* Worker-collision gate (2026-05-28 fix): master_client_id ∈ {1, 2}
  requires --worker-offline-confirmed; error message includes the
  operator stop/run/start ceremony.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.operator_tools.master_client_id_probe import (
    DEFAULT_CLEANUP_CLIENT_ID,
    DEFAULT_MARKET,
    DEFAULT_PLACE_CLIENT_ID,
    SAFE_STOP_PRICE,
    ParsedArgs,
    parse_args,
)


class TestParseArgs:
    def test_happy_path_paper(self) -> None:
        # master_client_id=1 is in the worker-allocation range so
        # --worker-offline-confirmed is required (2026-05-28 gate).
        result = parse_args(
            [
                "--master-client-id",
                "1",
                "--env",
                "paper",
                "--worker-offline-confirmed",
            ]
        )
        assert isinstance(result, ParsedArgs)
        assert result.master_client_id == 1
        assert result.place_client_id == DEFAULT_PLACE_CLIENT_ID
        assert result.cleanup_client_id == DEFAULT_CLEANUP_CLIENT_ID
        assert result.market == DEFAULT_MARKET
        assert result.env == "paper"
        assert result.allow_non_paper is False
        assert result.worker_offline_confirmed is True

    def test_overrides_placing_and_cleanup_cids(self) -> None:
        result = parse_args(
            [
                "--master-client-id",
                "2",
                "--place-client-id",
                "88",
                "--cleanup-client-id",
                "89",
                "--market",
                "/MNQ",
                "--env",
                "paper",
                "--worker-offline-confirmed",
            ]
        )
        assert result.master_client_id == 2
        assert result.place_client_id == 88
        assert result.cleanup_client_id == 89
        assert result.market == "/MNQ"

    def test_place_and_master_must_differ(self) -> None:
        # master=86 keeps the collision-with-place check focused on the
        # right error message rather than tripping the worker gate first.
        with pytest.raises(
            ValueError, match="--place-client-id must differ from --master-client-id"
        ):
            parse_args(
                [
                    "--master-client-id",
                    "86",
                    "--place-client-id",
                    "86",
                    "--env",
                    "paper",
                ]
            )

    def test_cleanup_and_master_must_differ(self) -> None:
        with pytest.raises(
            ValueError, match="--cleanup-client-id must differ from --master-client-id"
        ):
            parse_args(
                [
                    "--master-client-id",
                    "87",
                    "--cleanup-client-id",
                    "87",
                    "--env",
                    "paper",
                ]
            )

    def test_cleanup_and_place_must_differ(self) -> None:
        with pytest.raises(
            ValueError, match="--cleanup-client-id must differ from --place-client-id"
        ):
            parse_args(
                [
                    "--master-client-id",
                    "1",
                    "--place-client-id",
                    "86",
                    "--cleanup-client-id",
                    "86",
                    "--env",
                    "paper",
                    "--worker-offline-confirmed",
                ]
            )

    def test_place_cid_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be in 80-99"):
            parse_args(
                [
                    "--master-client-id",
                    "1",
                    "--place-client-id",
                    "5",
                    "--env",
                    "paper",
                    "--worker-offline-confirmed",
                ]
            )

    def test_cleanup_cid_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be in 80-99"):
            parse_args(
                [
                    "--master-client-id",
                    "1",
                    "--cleanup-client-id",
                    "100",
                    "--env",
                    "paper",
                    "--worker-offline-confirmed",
                ]
            )

    def test_live_env_without_allow_non_paper_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(
                [
                    "--master-client-id",
                    "1",
                    "--env",
                    "live-small",
                    "--worker-offline-confirmed",
                ]
            )

    def test_live_env_with_allow_non_paper_accepted(self) -> None:
        result = parse_args(
            [
                "--master-client-id",
                "1",
                "--env",
                "live-scale",
                "--allow-non-paper",
                "--worker-offline-confirmed",
            ]
        )
        assert result.env == "live-scale"
        assert result.allow_non_paper is True


class TestWorkerCollisionGate:
    """2026-05-28 gate — master_client_id ∈ {1, 2} requires
    --worker-offline-confirmed because IBKR refuses concurrent
    second connections on a clientId already held by the api worker.

    Without this gate the probe's Stage 2 connect would fail with
    Error 162 and wedge the colliding client for ~30 min. See module
    docstring "Worker-collision gate" subsection.
    """

    def test_master_cid_1_without_flag_rejected(self) -> None:
        with pytest.raises(ValueError, match="--worker-offline-confirmed") as exc_info:
            parse_args(["--master-client-id", "1", "--env", "paper"])
        # Error message should give the operator the exact recovery
        # ceremony so they can copy-paste it.
        msg = str(exc_info.value)
        assert "docker compose" in msg
        assert "stop api" in msg
        assert "start api" in msg
        assert "02:00-04:00 UTC" in msg

    def test_master_cid_2_without_flag_rejected(self) -> None:
        # ClientId=2 is the VPS deploy override of the worker; same gate.
        with pytest.raises(ValueError, match="--worker-offline-confirmed"):
            parse_args(["--master-client-id", "2", "--env", "paper"])

    def test_master_cid_1_with_flag_accepted(self) -> None:
        result = parse_args(
            [
                "--master-client-id",
                "1",
                "--env",
                "paper",
                "--worker-offline-confirmed",
            ]
        )
        assert result.master_client_id == 1
        assert result.worker_offline_confirmed is True

    def test_master_cid_outside_worker_range_does_not_need_flag(self) -> None:
        # ClientId=85 is in the operator-tools range, distinct from the
        # default place (86) and cleanup (87) clientIds — no collision,
        # no worker overlap; the flag is not required. This branch
        # supports future use cases where master is moved off the worker
        # (e.g., a dedicated reconciliation-only clientId).
        result = parse_args(["--master-client-id", "85", "--env", "paper"])
        assert result.master_client_id == 85
        assert result.worker_offline_confirmed is False


def test_safe_stop_price_is_decimal_one() -> None:
    """The probe's safe stop price is a Decimal $1.00 — well below any
    realistic /MES tick. Locks against accidental float drift."""
    assert SAFE_STOP_PRICE == Decimal("1")
    assert isinstance(SAFE_STOP_PRICE, Decimal)
