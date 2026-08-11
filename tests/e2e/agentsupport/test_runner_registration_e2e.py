"""End-to-end: a standalone Runner registers and executes tasks for the control plane."""

import pytest
from httpx import ASGITransport

from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.core_runtime import TraeCoreRunnerRuntime
from agentsupport.services import AgentSupportService
from session_runner.registration import RunnerRegistrationClient
from session_runner.server import create_runner_app


@pytest.mark.asyncio
async def test_registered_runner_executes_task_end_to_end(tmp_path):
    service = AgentSupportService(
        Settings(workspace_root=tmp_path / "workspaces", runner_token="e2e-secret")
    )
    control_app = create_app(service)

    runner_app = create_runner_app(runner_mode="deterministic")
    client = RunnerRegistrationClient(
        control_plane_url="http://control.test",
        bootstrap_token="e2e-secret",
        endpoint="http://runner.test:8080",
        provider="deterministic",
        transport=ASGITransport(app=control_app),
    )
    assert await client.register() is True
    assert service.runner_snapshot()[0]["type"] == "deterministic"

    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner.test", transport=ASGITransport(app=runner_app)
    )
    workspace = service.create_workspace("e2e")
    session = service.create_session(
        workspace.id, tenant_id="t-1", user_id="u-1", project_id="p-1"
    )
    conversation = await service.create_conversation(session.id, "hello e2e")

    assert conversation.run.state == "COMPLETED"
    events = service.events(conversation.id)
    assert [event.type for event in events][-1] == "run.completed"
    assert all(event.tenant_id == "t-1" for event in events)
    assert all(event.project_id == "p-1" for event in events)

    assert await client.deregister() is True
    assert service.runner_snapshot() == []
