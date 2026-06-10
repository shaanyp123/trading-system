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


def test_select_backend_prefers_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker is the default: the only backend that mounts/guards the data COPY and
    # carries the isolation env. An explicit prefer is still honoured.
    monkeypatch.setattr(images, "lean_cli_available", lambda: True)
    monkeypatch.setattr(images, "docker_available", lambda: True)
    assert images.select_backend() == "docker"
    assert images.select_backend("lean_cli") == "lean_cli"
    assert images.select_backend("docker") == "docker"


def test_select_backend_falls_back_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker backend unusable, CLI runnable ⇒ the CLI is the fallback.
    monkeypatch.setattr(images, "docker_backend_runnable", lambda: False)
    monkeypatch.setattr(images, "cli_backend_runnable", lambda: True)
    assert images.select_backend() == "lean_cli"


def test_select_backend_docker_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "lean_cli_available", lambda: False)
    monkeypatch.setattr(images, "docker_available", lambda: True)
    assert images.select_backend() == "docker"
    # CLI needs docker too; absent CLI ⇒ prefer='lean_cli' yields None.
    assert images.select_backend("lean_cli") is None


def test_docker_image_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/docker")

    def fake_run(args: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        rc = 0 if args[-1] == "present:latest" else 1
        return subprocess.CompletedProcess(args=args, returncode=rc, stdout="", stderr="")

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.docker_image_available("present:latest") is True
    assert images.docker_image_available("absent:latest") is False


def test_runnable_backend_requires_image_for_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker daemon up, CLI absent, image NOT built ⇒ no runnable backend (→ skip),
    # even though select_backend() would optimistically return "docker".
    monkeypatch.setattr(images, "lean_cli_available", lambda: False)
    monkeypatch.setattr(images, "docker_available", lambda: True)
    monkeypatch.setattr(images, "docker_image_available", lambda _img: False)
    assert images.select_backend() == "docker"  # daemon-only optimism
    assert images.runnable_backend(lean_local_image="x:latest") is None  # image-aware → skip
    # ...image present ⇒ docker is runnable.
    monkeypatch.setattr(images, "docker_image_available", lambda _img: True)
    assert images.runnable_backend(lean_local_image="x:latest") == "docker"


def test_runnable_backend_prefers_docker_over_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both runnable ⇒ docker (data mount + isolation env); image absent ⇒ the CLI
    # is the fallback.
    monkeypatch.setattr(images, "lean_cli_available", lambda: True)
    monkeypatch.setattr(images, "docker_available", lambda: True)
    monkeypatch.setattr(images, "docker_image_available", lambda _img: True)
    assert images.runnable_backend(lean_local_image="x:latest") == "docker"
    monkeypatch.setattr(images, "docker_image_available", lambda _img: False)
    assert images.runnable_backend(lean_local_image="x:latest") == "lean_cli"


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


def test_build_docker_command_optional_name_flag() -> None:
    named = images.build_docker_command(
        "img:latest",
        mounts=[],
        env={},
        command=["dotnet"],
        docker_exe="docker",
        name="research-lean-x",
    )
    assert named[named.index("--name") + 1] == "research-lean-x"
    unnamed = images.build_docker_command(
        "img:latest", mounts=[], env={}, command=["dotnet"], docker_exe="docker"
    )
    assert "--name" not in unnamed
