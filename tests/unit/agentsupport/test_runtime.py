import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentsupport.runtime import DockerCliRuntimeDriver, default_session_container_env


class FakeDockerCliRuntimeDriver(DockerCliRuntimeDriver):
    def __init__(self, container_name: str):
        super().__init__(startup_timeout_seconds=1, container_env={"SESSION_RUNNER_MODE": "trae"})
        self.container_name = container_name
        self.container_id = "container-id"
        self.running = False
        self.removed = False
        self.commands: list[tuple[str, ...]] = []

    async def _command(self, *args: str, check: bool = True) -> str:
        self.commands.append(args)
        if args[0] == "inspect":
            if self.removed:
                return "[]"
            if not self.running and not self.removed:
                return "[]"
            status = "running" if self.running else "exited"
            return json.dumps(
                [
                    {
                        "Id": self.container_id,
                        "State": {"Status": status},
                        "Config": {"Labels": {"session_id": "session"}},
                        "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "49152"}]}},
                        "Mounts": [],
                    }
                ]
            )
        if args[0] == "run":
            self.running = True
            return self.container_id
        if args[0] == "stop":
            self.running = False
        if args[0] == "rm":
            self.removed = True
        return ""

    async def _wait_ready(self, endpoint: str) -> None:
        return None


@pytest.mark.asyncio
async def test_docker_cli_driver_enforces_session_security_and_endpoint(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = tmp_path / "skill"
    skill.mkdir()
    driver = FakeDockerCliRuntimeDriver("agentsupport-runner-session-1")

    container_id = await driver.start(
        uuid4(),
        str(workspace),
        4,
        uuid4(),
        [(str(skill), "/opt/agent-skills/review")],
        runtime_operation_id=uuid4(),
    )

    run_command = next(command for command in driver.commands if command[0] == "run")
    assert container_id == "container-id"
    assert "--read-only" in run_command
    assert "--cap-drop=ALL" in run_command
    assert "--security-opt=no-new-privileges" in run_command
    assert "--user" in run_command and "agent" in run_command
    assert "--publish" in run_command
    assert "127.0.0.1::8080" in run_command
    assert "--env" in run_command and "SESSION_RUNNER_MODE=trae" in run_command
    assert "SESSION_LEASE_EPOCH=4" in run_command
    assert any("target=/opt/agent-skills/review,readonly" in item for item in run_command)
    assert await driver.endpoint(container_id) == "http://127.0.0.1:49152"

    assert await driver.stop(container_id)
    assert await driver.endpoint(container_id) is None


@pytest.mark.asyncio
async def test_docker_cli_driver_removes_container_that_exited_before_stop():
    class AlreadyExitedDriver(FakeDockerCliRuntimeDriver):
        async def _command(self, *args: str, check: bool = True) -> str:
            if args[0] == "inspect" and not self.removed:
                return json.dumps(
                    [
                        {
                            "Id": self.container_id,
                            "State": {"Status": "exited"},
                            "Config": {"Labels": {}},
                            "NetworkSettings": {"Ports": {}},
                            "Mounts": [],
                        }
                    ]
                )
            return await super()._command(*args, check=check)

    driver = AlreadyExitedDriver("agentsupport-runner-exited")

    assert await driver.stop("container-id")
    assert driver.removed is True
    assert ("rm", "-f", "container-id") in driver.commands


@pytest.mark.asyncio
async def test_docker_cli_driver_escalates_sigterm_to_sigkill_after_grace_period():
    class GracefulTimeoutDriver(FakeDockerCliRuntimeDriver):
        async def _command(self, *args: str, check: bool = True) -> str:
            if args[0] == "stop":
                self.commands.append(args)
                return ""
            if args[0] == "kill":
                self.commands.append(args)
                self.running = False
                return ""
            return await super()._command(*args, check=check)

    driver = GracefulTimeoutDriver("agentsupport-runner-timeout")
    driver.running = True

    assert await driver.stop("container-id")
    command_names = [command[0] for command in driver.commands]
    assert command_names == ["inspect", "stop", "inspect", "kill", "inspect", "rm"]


def test_default_session_container_env_only_contains_model_and_runner_settings(monkeypatch):
    monkeypatch.delenv("SESSION_RUNNER_MODE", raising=False)
    monkeypatch.setenv("TRAE_PROVIDER", "openai")
    monkeypatch.setenv("TRAE_API_KEY", "temporary")
    monkeypatch.setenv("TRAE_MODEL", "model")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    environment = default_session_container_env()

    assert environment["SESSION_RUNNER_MODE"] == "trae"
    assert environment["TRAE_PROVIDER"] == "openai"
    assert environment["TRAE_API_KEY"] == "temporary"
    assert environment["TRAE_MODEL"] == "model"
    assert "DATABASE_URL" not in environment


def test_default_session_container_env_maps_anthropic_compatibility_aliases(monkeypatch):
    monkeypatch.delenv("TRAE_API_KEY", raising=False)
    monkeypatch.delenv("TRAE_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "temporary-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.invalid/")

    environment = default_session_container_env()

    assert environment["TRAE_API_KEY"] == "temporary-token"
    assert environment["TRAE_MODEL_BASE_URL"] == "https://provider.invalid/"
