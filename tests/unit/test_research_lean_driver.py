"""LEAN driver assembly + isolation invariants (design §4.3 driver.py, §9 P2).

The subprocess is skip-gated (no LEAN here), but the command/env/mount ASSEMBLY is
pure and unit-tested — especially the anti-pollution guarantees: a research V1
backtest must never reach the prod api or mount the live data volume.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research.lean import driver, images
from research.lean.driver import (
    LeanRunError,
    LeanRunSpec,
    assemble_docker_run,
    build_run_argv,
    isolation_env,
    new_work_dir,
    run_backtest,
)


def _spec(tmp_path: Path, **overrides: object) -> LeanRunSpec:
    data_root = tmp_path / "lean_bars"
    data_root.mkdir(exist_ok=True)
    algo = tmp_path / "donchian_reference.py"
    algo.write_text("# reference algo\n", encoding="utf-8")
    base: dict[str, object] = {
        "algorithm_type_name": "DonchianReferenceAlgorithm",
        "algorithm_source": algo,
        "parameters": {"DONCHIAN_CHANNEL": "20", "RESEARCH_SYMBOL": "TLT"},
        "data_root": data_root,
        "symbol": "TLT",
        "multiplier": 1.0,
        "strategy_name": "donchian(20,1)",
    }
    base.update(overrides)
    return LeanRunSpec(**base)  # type: ignore[arg-type]


def test_isolation_env_neutralizes_post_hazard() -> None:
    env = isolation_env()
    assert env["LEAN_LOCAL_API_BASE_URL"] == "http://127.0.0.1:9"  # unreachable stub
    assert env["LEAN_LOCAL_BEARER_TOKEN"]  # non-empty → initialize() won't fail-close
    assert env["LEAN_LOCAL_BEARER_TOKEN"] != ""
    assert env["LEAN_LIVE_MODE"] == "false"
    # Must NOT be the prod api.
    assert "api:8000" not in env["LEAN_LOCAL_API_BASE_URL"]


def test_guard_data_root_refuses_live_volume(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="live data volume"):
        driver._guard_data_root(Path("trading_lean_data"))
    with pytest.raises(ValueError, match="live data volume"):
        driver._guard_data_root(Path("lean_data"))


def test_guard_data_root_requires_existing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        driver._guard_data_root(tmp_path / "missing")


def test_guard_data_root_refuses_live_project_volume_name() -> None:
    # The docker-compose live project prefixes the volume: trading-live_lean_data.
    with pytest.raises(ValueError, match="live data volume"):
        driver._guard_data_root(Path("trading-live_lean_data"))


def test_guard_data_root_refuses_symlink_into_forbidden_name(tmp_path: Path) -> None:
    # An innocuously named symlink that RESOLVES into a live-volume-named dir must
    # be refused — the pre-resolve name check alone would let it through.
    real = tmp_path / "trading_lean_data"
    real.mkdir()
    link = tmp_path / "innocent_copy"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="live data volume"):
        driver._guard_data_root(link)


def test_guard_data_root_refuses_docker_volumes_storage() -> None:
    # Anything under /var/lib/docker/volumes is some container's volume — never a COPY.
    with pytest.raises(ValueError, match="docker-managed volume"):
        driver._guard_data_root(Path("/var/lib/docker/volumes/whatever/_data"))


def test_assemble_docker_run_mounts_and_env(tmp_path: Path) -> None:
    spec = _spec(tmp_path, extra_package_mounts=(tmp_path,))
    project = tmp_path / "project"
    output = tmp_path / "output"
    output.mkdir()
    assembled = assemble_docker_run(spec, project, output)

    targets = {m.target: m for m in assembled.mounts}
    assert targets["/Lean/Data"].read_only is True
    assert targets["/Lean/Algorithm/lean.json"].read_only is True
    assert targets["/Lean/Algorithm/v1_strategy.py"].read_only is True  # entrypoint hard-codes path
    assert targets["/Results"].read_only is False  # LEAN writes results here
    assert "/Lean/strategies" in targets
    assert assembled.env == isolation_env()
    # The data mount source is the COPY, never the live volume.
    data_src = targets["/Lean/Data"].source
    assert data_src.name == "lean_bars"
    assert data_src.name not in ("lean_data", "trading_lean_data")


def test_build_docker_argv_is_isolated_and_post_safe(tmp_path: Path) -> None:
    spec = _spec(tmp_path, posts_to_api=True, extra_package_mounts=(tmp_path,))
    argv = build_run_argv(
        spec,
        tmp_path / "project",
        tmp_path / "out",
        backend="docker",
        image="trading-lean-local:latest",
    )
    joined = " ".join(argv)
    # Isolation: no prod network, POST stub, dummy bearer.
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "LEAN_LOCAL_API_BASE_URL=http://127.0.0.1:9" in argv
    assert any(a.startswith("LEAN_LOCAL_BEARER_TOKEN=") for a in argv)
    # Anti-pollution: the prod api + live volume never appear anywhere in the argv.
    assert "api:8000" not in joined
    assert "trading_lean_data" not in joined
    assert "lean_data:/Lean/Data" not in joined  # named-volume mount form
    assert argv[-2:] == ["dotnet", "/Lean/Launcher/bin/Debug/QuantConnect.Lean.Launcher.dll"]


def test_assemble_docker_run_refuses_forbidden_output_dir(tmp_path: Path) -> None:
    # The rw /Results mount gets the same resolved-parts guard as the data root.
    spec = _spec(tmp_path)
    out = tmp_path / "trading_lean_data"
    out.mkdir()
    with pytest.raises(ValueError, match="output_dir"):
        assemble_docker_run(spec, tmp_path / "project", out)


def test_assemble_docker_run_requires_algorithm_file(tmp_path: Path) -> None:
    # A docker bind mount of a MISSING source silently creates an empty dir in the
    # container — must fail loud instead, with the CWD hint.
    spec = _spec(tmp_path, algorithm_source=tmp_path / "missing.py")
    with pytest.raises(FileNotFoundError, match="repo root"):
        assemble_docker_run(spec, tmp_path / "project", tmp_path / "out")


def test_assemble_docker_run_requires_package_mount_dirs(tmp_path: Path) -> None:
    spec = _spec(tmp_path, extra_package_mounts=(tmp_path / "no_such_pkg",))
    with pytest.raises(FileNotFoundError, match="repo root"):
        assemble_docker_run(spec, tmp_path / "project", tmp_path / "out")
    spec = _spec(tmp_path, service_package_mount=tmp_path / "no_such_services")
    with pytest.raises(FileNotFoundError, match="repo root"):
        assemble_docker_run(spec, tmp_path / "project", tmp_path / "out")


def test_assemble_docker_run_rejects_multiple_strategy_mounts(tmp_path: Path) -> None:
    # Every extra_package_mounts entry binds the SAME /Lean/strategies target —
    # docker would let the last -v silently win.
    pkg_a = tmp_path / "pkg_a"
    pkg_b = tmp_path / "pkg_b"
    pkg_a.mkdir()
    pkg_b.mkdir()
    spec = _spec(tmp_path, extra_package_mounts=(pkg_a, pkg_b))
    with pytest.raises(ValueError, match="at most one"):
        assemble_docker_run(spec, tmp_path / "project", tmp_path / "out")


def test_build_docker_argv_carries_container_name(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    argv = build_run_argv(
        spec,
        tmp_path / "project",
        tmp_path / "out",
        backend="docker",
        image="trading-lean-local:latest",
        container_name="research-lean-work",
    )
    assert argv[argv.index("--name") + 1] == "research-lean-work"


def test_build_cli_argv(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    argv = build_run_argv(spec, tmp_path / "project", tmp_path / "out", backend="lean_cli")
    assert argv[1] == "backtest"
    assert "--output" in argv


def test_build_run_argv_rejects_posts_to_api_on_cli_backend(tmp_path: Path) -> None:
    # The argv builder itself refuses the combination (not just run_backtest) so a
    # direct build_run_argv caller cannot stage an un-isolated V1 run.
    spec = _spec(tmp_path, posts_to_api=True)
    with pytest.raises(LeanRunError, match="posts_to_api"):
        build_run_argv(spec, tmp_path / "project", tmp_path / "out", backend="lean_cli")


def test_run_backtest_skips_when_no_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images, "lean_cli_available", lambda: False)
    monkeypatch.setattr(images, "docker_available", lambda: False)
    with pytest.raises(images.LeanUnavailableError):
        run_backtest(_spec(tmp_path), work_dir=tmp_path / "work")


def test_run_backtest_v1_rejects_cli_backend(tmp_path: Path) -> None:
    # V1 POSTs; the POST-stub env is only wired through docker, so CLI is refused.
    with pytest.raises(LeanRunError, match="posts_to_api"):
        run_backtest(
            _spec(tmp_path, posts_to_api=True), work_dir=tmp_path / "work", backend="lean_cli"
        )


def test_run_backtest_timeout_removes_container_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0, output="partial-stdout")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    with pytest.raises(LeanRunError, match="timed out after") as excinfo:
        run_backtest(spec, work_dir=tmp_path / "work", backend="docker", timeout_s=1.0)
    # The partial output is surfaced in the error tail.
    assert "partial-stdout" in str(excinfo.value)
    # First call: the docker run carries the deterministic container name; second
    # call: the best-effort `docker rm -f <name>` cleanup (its own TimeoutExpired
    # is swallowed — the timeout error above is what the caller must see).
    assert calls[0][calls[0].index("--name") + 1] == "research-lean-work"
    assert calls[1][1:4] == ["rm", "-f", "research-lean-work"]


def test_run_backtest_failure_tail_carries_both_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)

    def fake_run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout="stdout-clue", stderr="stderr-clue"
        )

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    with pytest.raises(LeanRunError, match="exited 1") as excinfo:
        run_backtest(spec, work_dir=tmp_path / "work", backend="docker")
    msg = str(excinfo.value)
    # Both labeled streams — a non-empty stderr must not hide the stdout clue.
    assert "stderr-clue" in msg and "stdout-clue" in msg


def test_run_backtest_missing_result_raises_lean_run_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero exit + no result document ⇒ LeanRunError (the docstring contract), not a
    # bare FileNotFoundError from the parser.
    spec = _spec(tmp_path)

    def fake_run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    with pytest.raises(LeanRunError, match="no parseable result"):
        run_backtest(spec, work_dir=tmp_path / "work", backend="docker")


def test_new_work_dir_same_second_collision_gets_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Frozen:
        @staticmethod
        def now(tz: object) -> datetime:
            return datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(driver, "datetime", _Frozen)
    first = new_work_dir(tmp_path)
    second = new_work_dir(tmp_path)
    assert first.is_dir() and second.is_dir() and first != second
    assert second.name == f"{first.name}-01"
