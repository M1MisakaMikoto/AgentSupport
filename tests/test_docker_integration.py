from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_platform.config import Settings
from agent_platform.domain import ExecutionState
from agent_platform.services import PlatformService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_INTEGRATION") != "1",
    reason="set RUN_DOCKER_INTEGRATION=1 to run real Docker acceptance tests",
)


def _service(root: Path, **overrides: object) -> PlatformService:
    values: dict[str, object] = {
        "workspace_root": root,
        "runtime_driver": "docker_cli",
        "runtime_context": "desktop-linux",
        "core_runner_url": None,
        "runtime_start_timeout_seconds": 45,
        "runtime_stop_grace_seconds": 10,
        "health_failure_threshold": 1,
        "max_active_sessions": 1,
    }
    values.update(overrides)
    return PlatformService(Settings(**values))


@pytest.mark.asyncio
async def test_real_docker_fifo_lease_and_cleanup(tmp_path: Path):
    service = _service(tmp_path)
    workspace = service.create_workspace("fifo")
    first = service.create_session(workspace.id)
    first_run = await service.create_conversation(first.id, "ask: hold first")
    first_container = first.active_container_id
    second = service.create_session(workspace.id)
    second_run = await service.create_conversation(second.id, "ask: queued second")

    assert second_run.run.state == ExecutionState.QUEUED
    first_run = await service.submit_input(
        first_run.id, first_run.run.pending_interaction["interaction_id"], "continue"
    )
    second_run = service.conversations[second_run.id]
    second_container = second.active_container_id
    second_run = await service.submit_input(
        second_run.id, second_run.run.pending_interaction["interaction_id"], "continue"
    )

    assert first_run.run.state == ExecutionState.COMPLETED
    assert second_run.run.state == ExecutionState.COMPLETED
    assert await service.runtime_driver.inspect(first_container) == {"status": "missing"}
    assert await service.runtime_driver.inspect(second_container) == {"status": "missing"}


@pytest.mark.asyncio
async def test_real_docker_crash_becomes_lost_and_is_removed(tmp_path: Path):
    service = _service(tmp_path)
    workspace = service.create_workspace("crash")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "ask: hold until crash")
    container_id = session.active_container_id

    await service.runtime_driver._command("kill", container_id, check=False)
    assert await service.supervise_active_sessions() == 1
    assert conversation.run.state == ExecutionState.LOST
    assert session.active_container_id is None
    assert await service.runtime_driver.inspect(container_id) == {"status": "missing"}


@pytest.mark.asyncio
async def test_real_docker_security_and_skill_mounts(tmp_path: Path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("authorized skill\n", encoding="utf-8")
    service = _service(tmp_path / "workspaces", skills_root=skills_root, enabled_skills="review")
    workspace = service.create_workspace("security")
    session = service.create_session(workspace.id)
    await service._acquire_container(session)
    container_id = session.active_container_id
    try:
        raw = json.loads(await service.runtime_driver._command("inspect", container_id))[0]
        host = raw["HostConfig"]
        config = raw["Config"]
        mounts = raw["Mounts"]
        assert config["User"] == "agent"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["PidsLimit"] == 256
        assert host["Memory"] == 2 * 1024 * 1024 * 1024
        assert host["NanoCpus"] == 2 * 1000 * 1000 * 1000
        assert "/tmp" in host["Tmpfs"]
        assert any(
            mount["Destination"] == "/opt/agent-skills/review" and mount["RW"] is False
            for mount in mounts
        )
        endpoint = await service.runtime_driver.endpoint(container_id)
        async with httpx.AsyncClient(base_url=endpoint) as client:
            stale = await client.post(
                "/runs",
                json={
                    "run_id": str(uuid4()),
                    "conversation_id": str(uuid4()),
                    "session_id": str(session.id),
                    "container_id": container_id,
                    "lease_epoch": 2,
                    "fence_epoch": 2,
                    "correlation_id": "stale-lease",
                    "context_bundle": {"task": "complete"},
                },
            )
        assert stale.status_code == 409
        assert (
            await service.runtime_driver._command(
                "exec", container_id, "sh", "-c", "cat /opt/agent-skills/review/SKILL.md"
            )
            == "authorized skill"
        )
        await service.runtime_driver._command(
            "exec",
            container_id,
            "sh",
            "-c",
            "echo denied >> /opt/agent-skills/review/SKILL.md",
            check=False,
        )
        assert (
            await service.runtime_driver._command(
                "exec", container_id, "sh", "-c", "cat /opt/agent-skills/review/SKILL.md"
            )
            == "authorized skill"
        )
    finally:
        await service.runtime_driver.stop(container_id)


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_DOCKER_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_DOCKER_INTEGRATION=1 with Compose PostgreSQL",
)
@pytest.mark.asyncio
async def test_real_postgres_restart_recovers_active_session(tmp_path: Path):
    config = {
        "database_url": "postgresql+psycopg://agent:agent@localhost:5432/agent_platform",
        "persistence_mode": "postgres",
    }
    first = _service(tmp_path, **config)
    workspace = first.create_workspace("postgres-restart")
    session = first.create_session(workspace.id)
    conversation = await first.create_conversation(session.id, "ask: survive restart")
    container_id = session.active_container_id

    restarted = _service(tmp_path, **config)
    restored = restarted._conversation(conversation.id)
    endpoint = await restarted.runtime_driver.endpoint(container_id)
    assert await restarted.supervise_active_sessions() == 0
    restored = await restarted.submit_input(
        restored.id, restored.run.pending_interaction["interaction_id"], "continue"
    )

    assert endpoint
    assert restored.run.state == ExecutionState.COMPLETED
    assert restarted.sessions[session.id].active_container_id is None
    assert await restarted.runtime_driver.inspect(container_id) == {"status": "missing"}
    assert restarted.events(restored.id)[-1].type == "run.completed"
