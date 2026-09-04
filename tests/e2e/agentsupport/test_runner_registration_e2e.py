"""End-to-end: a standalone Runner registers and executes tasks for the control plane."""

import pytest

from agent_runner_contracts.registration import (
    RUNNER_CAPABILITIES,
    RunnerHeartbeat,
    RunnerRegistrationRequest,
)
from _support import make_temporal_service as _make_service


@pytest.mark.asyncio
async def test_registered_runner_executes_task_end_to_end(tmp_path):
    env = _make_service(tmp_path, runner_token="e2e-secret")
    service = env.service
    response = service.register_runner(
        RunnerRegistrationRequest(
            provider="trae",
            endpoint="http://runner.test:8080",
            version="0.1.0",
            capabilities=list(RUNNER_CAPABILITIES),
        ),
        bootstrap_token="e2e-secret",
    )
    service.runner_heartbeat(
        response.runner_id,
        RunnerHeartbeat(status="READY", load=1),
        runner_token=response.token,
    )
    assert service.runner_snapshot()[0]["type"] == "trae"

    workspace = service.create_workspace("e2e")
    session = service.create_session(
        workspace.id, tenant_id="t-1", user_id="u-1", project_id="p-1"
    )
    conversation = await service.create_conversation(session.id, "hello e2e")
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=20)

    assert env.fake.captured[0]["runner_url"] == "http://runner.test:8080"
    events = env.repository.list_events(conversation.id)
    assert events[-1].type == "run.completed"
    assert session.tenant_id == "t-1"
    assert session.project_id == "p-1"

    service.runner_registry.deregister(response.runner_id)
    assert service.runner_snapshot() == []
