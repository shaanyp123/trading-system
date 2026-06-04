"""LEAN invocation backends + availability checks (design §4.3 images.py, P2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from research.lean import images
from research.lean.images import LeanUnavailableError, Mount


def test_lean_cli_available_probes_executable_not_importlib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The empty `lean` namespace-package artifact makes find_spec("lean") truthy in
    # some venvs; availability MUST key off the console script instead.
    monkeypatch.setattr(images.shutil, "which", lambda _name: None)
    assert images.lean_cli_available() is False
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/lean")
    assert images.lean_cli_available() is True


def test_docker_available_false_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images.shutil, "which", lambda _name: None)
    assert images.docker_available() is False


def test_docker_available_true_when_daemon_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/docker")

    def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="24.0.7\n", stderr="")

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.docker_available() is True


def test_docker_available_false_on_daemon_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/docker")

    def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Cannot connect"
        )

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.docker_available() is False


def test_docker_available_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/docker")

    def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="docker info", timeout=10.0)

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.docker_available() is False


def test_select_backend_prefers_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "lean_cli_available", lambda: True)
    monkeypatch.setattr(images, "docker_available", lambda: True)
    assert images.select_backend() == "lean_cli"
    assert images.select_backend("docker") == "docker"


def test_select_backend_docker_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "lean_cli_available", lambda: False)
    monkeypatch.setattr(images, "docker_available", lambda: True)
    assert images.select_backend() == "docker"
    # CLI needs docker too; absent CLI ⇒ prefer='lean_cli' yields None.
    assert images.select_backend("lean_cli") is None


def test_select_backend_none_when_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "lean_cli_available", lambda: False)
    monkeypatch.setattr(images, "docker_available", lambda: False)
    assert images.select_backend() is None
    with pytest.raises(LeanUnavailableError, match="no LEAN backend"):
        images.require_backend()


def test_build_cli_command() -> None:
    cmd = images.build_cli_command(
        Path("/tmp/proj"), Path("/tmp/out"), image="quantconnect/lean:latest", lean_exe="lean"
    )
    assert cmd[:2] == ["lean", "backtest"]
    assert "/tmp/proj" in cmd
    assert cmd[cmd.index("--output") + 1] == "/tmp/out"
    assert cmd[cmd.index("--image") + 1] == "quantconnect/lean:latest"


def test_mount_to_flag() -> None:
    assert Mount(Path("/host/data"), "/Lean/Data", read_only=True).to_flag() == (
        "/host/data:/Lean/Data:ro"
    )
    assert Mount(Path("/host/out"), "/Results", read_only=False).to_flag() == "/host/out:/Results"


def test_build_docker_command_structure() -> None:
    cmd = images.build_docker_command(
        "trading-lean-local:latest",
        mounts=[Mount(Path("/host/data"), "/Lean/Data", read_only=True)],
        env={"B": "2", "A": "1"},
        command=["dotnet", "x.dll"],
        network="none",
        docker_exe="docker",
    )
    assert cmd[:5] == ["docker", "run", "--rm", "--network", "none"]
    # Env sorted for deterministic argv.
    assert cmd.index("-e") < cmd.index("trading-lean-local:latest")
    a_idx = cmd.index("A=1")
    b_idx = cmd.index("B=2")
    assert a_idx < b_idx
    assert "/host/data:/Lean/Data:ro" in cmd
    # Image precedes the container command.
    img_idx = cmd.index("trading-lean-local:latest")
    assert cmd[img_idx + 1 :] == ["dotnet", "x.dll"]
