"""Host-side runner manager: preset builds and on-demand runners (fake Docker)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from runner_manager.manager import CommandResult, RunnerManager, render_dockerfile
from runner_manager.settings import ManagerSettings


class _FakeDocker:
    """Records argv and answers the two inspect calls the manager makes."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if " build " in f" {joined} ":
            return CommandResult(0, stdout="build log line\n", stderr="")
        if " run -d " in f" {joined} ":
            return CommandResult(0, stdout="container-abc\n")
        if "inspect" in argv and "Ports" in joined:
            return CommandResult(0, stdout="49153\n")
        if "inspect" in argv and "State.Running" in joined:
            return CommandResult(0, stdout="true\n")
        if argv[:1] == ["docker"] and argv[1:2] == ["rm"]:
            return CommandResult(0, stdout="container-abc\n")
        if argv[1:2] == ["rm"]:
            return CommandResult(0, stdout="container-abc\n")
        return CommandResult(0, stdout="")


def _platform_transport() -> tuple[httpx.MockTransport, dict[str, Any]]:
    state: dict[str, Any] = {"claimed": False, "completed": []}
    build_id = "11111111-1111-1111-1111-111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/internal/preset-builds/claim"):
            if state["claimed"]:
                return httpx.Response(200, json={"build": None})
            state["claimed"] = True
            return httpx.Response(
                200,
                json={
                    "build": {
                        "build_id": build_id,
                        "tenant_id": "acme",
                        "status": "RUNNING",
                        "content_hash": "hash",
                    },
                    "image_tag": "agentsupport-runner:acme-hash",
                    "cli_apps": [
                        {
                            "cli_id": "mytool",
                            "entry": "mytool",
                            "package": "mytool.tar.gz",
                            "safe_prefixes": ["mytool list"],
                            "env": ["MYTOOL_TOKEN"],
                            "package_b64": base64.b64encode(b"payload").decode(),
                        }
                    ],
                    "env": ["MYTOOL_TOKEN"],
                },
            )
        if path.endswith("/complete"):
            state["completed"].append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"status": "READY"})
        if path.endswith("/ready"):
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    return httpx.MockTransport(handler), state


def test_render_dockerfile_includes_packages_and_path():
    rendered = render_dockerfile(
        "agentsupport-api",
        [{"cli_id": "mytool", "entry": "mytool"}],
        ["MYTOOL_TOKEN"],
    )
    assert rendered.startswith("FROM agentsupport-api")
    assert "COPY packages /opt/cli" in rendered
    assert "/opt/cli/mytool" in rendered
    assert "MYTOOL_TOKEN" in rendered


def test_render_dockerfile_starts_declared_daemon():
    rendered = render_dockerfile(
        "agentsupport-api",
        [
            {
                "cli_id": "mytool",
                "entry": "mytool",
                "daemon": {"command": "mytool serve --port 9000", "port": 9000},
            }
        ],
        [],
    )
    assert 'ENTRYPOINT ["/opt/cli/entrypoint.sh"]' in rendered
    assert "nohup mytool serve --port 9000" in rendered
    assert "/tmp/mytool-daemon.log" in rendered
    assert 'exec "$@"' in rendered
    # The entrypoint must not depend on the base image CMD.
    assert "CMD [\"uvicorn\", \"session_runner.main:app\"" in rendered


def test_render_dockerfile_without_daemon_keeps_base_entrypoint():
    rendered = render_dockerfile("agentsupport-api", [{"cli_id": "x", "entry": "x"}], [])
    assert "ENTRYPOINT" not in rendered


@pytest.mark.asyncio
async def test_build_once_materialises_and_reports_ready(tmp_path: Path):
    transport, state = _platform_transport()
    docker = _FakeDocker()
    settings = ManagerSettings(
        platform_url="http://platform",
        token="secret",
        docker_transport="local",
        staging_root=tmp_path,
    )
    manager = RunnerManager(settings, command_runner=docker, transport=transport)

    assert await manager.build_once() is True
    assert state["completed"][0]["status"] == "READY"
    assert state["completed"][0]["image_tag"] == "agentsupport-runner:acme-hash"
    assert state["completed"][0]["log_tail"].strip() == "build log line"

    build_call = next(call for call in docker.calls if "build" in call)
    assert "agentsupport-runner:acme-hash" in build_call

    # The staging directory is cleaned up after the build.
    assert list(tmp_path.iterdir()) == []
    assert await manager.build_once() is False


@pytest.mark.asyncio
async def test_build_failure_is_reported_not_raised(tmp_path: Path):
    transport, state = _platform_transport()

    class FailingDocker(_FakeDocker):
        async def __call__(self, argv: list[str]) -> CommandResult:
            self.calls.append(list(argv))
            if "build" in argv:
                return CommandResult(1, stderr="no such base image")
            return await super().__call__(argv)

    settings = ManagerSettings(
        platform_url="http://platform",
        docker_transport="local",
        staging_root=tmp_path,
    )
    manager = RunnerManager(settings, command_runner=FailingDocker(), transport=transport)
    assert await manager.build_once() is True
    assert state["completed"][0]["status"] == "FAILED"
    assert "no such base image" in state["completed"][0]["log_tail"]


@pytest.mark.asyncio
async def test_ensure_runner_publishes_port_and_reap_idle(tmp_path: Path):
    transport, _state = _platform_transport()
    docker = _FakeDocker()
    settings = ManagerSettings(
        platform_url="http://platform",
        docker_transport="local",
        staging_root=tmp_path,
        endpoint_host="http://host.docker.internal",
        idle_seconds=60.0,
    )
    manager = RunnerManager(settings, command_runner=docker, transport=transport)

    entry = await manager.ensure_runner("acme", "agentsupport-runner:acme-hash")
    assert entry["endpoint"] == "http://host.docker.internal:49153"
    assert entry["container_id"] == "container-abc"
    run_call = next(call for call in docker.calls if "run" in call)
    assert "SESSION_RUNNER_TENANT_ID=acme" in run_call
    assert "agentsupport.tenant=acme" in run_call

    # A second call for the same tenant reuses the running container.
    docker.calls.clear()
    again = await manager.ensure_runner("acme", "agentsupport-runner:acme-hash")
    assert again["container_id"] == "container-abc"
    assert not any("run" in call for call in docker.calls)

    # Idle runners are reclaimed once the idle window passes.
    assert await manager.reap_idle(now=datetime.now(UTC)) == []
    later = datetime.now(UTC) + timedelta(seconds=120)
    assert await manager.reap_idle(now=later) == ["acme"]
    assert manager.runners() == []
