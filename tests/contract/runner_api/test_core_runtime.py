from uuid import uuid4

import pytest
from httpx import ASGITransport

from agentsupport.core_runtime import TraeCoreRunnerRuntime
from session_runner.server import create_runner_app


@pytest.mark.asyncio
async def test_core_runtime_http_contract_run_checkpoint_and_resume():
    runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    health = await runtime.health()
    assert health["live"]["status"] == "ok"
    run_id = uuid4()
    conversation_id = uuid4()
    received = []

    async def sink(event):
        received.append(event)

    result = await runtime.run(
        {
            "run_id": str(run_id),
            "conversation_id": str(conversation_id),
            "session_id": str(uuid4()),
            "container_id": "container-1",
            "lease_epoch": 7,
            "fence_epoch": 0,
            "correlation_id": "corr-1",
            "context_bundle": {"task": "ask: approve?"},
        },
        sink,
    )
    assert result["status"] == "WAITING_INPUT"
    assert received[-1].type == "interaction.requested"
    checkpoint = await runtime.checkpoint(run_id, "approval")
    resumed = await runtime.resume(checkpoint, "approved", sink)
    assert resumed["status"] == "COMPLETED"
    assert received[-1].type == "run.completed"


@pytest.mark.asyncio
async def test_core_runtime_forwards_approval():
    runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    run_id = uuid4()
    received = []

    async def sink(event):
        received.append(event)

    result = await runtime.run(
        {
            "run_id": str(run_id),
            "conversation_id": str(uuid4()),
            "session_id": str(uuid4()),
            "container_id": "container-1",
            "lease_epoch": 1,
            "correlation_id": "corr-approval",
            "context_bundle": {"task": "ask: approve tool?"},
        },
        sink,
    )
    approval_id = result["events"][-1]["payload"]["interaction_id"]
    approved = await runtime.accept_approval(run_id, approval_id, "APPROVE_ONCE")
    assert approved["status"] == "COMPLETED"
    assert approved["events"][-1]["type"] == "run.completed"


@pytest.mark.asyncio
async def test_core_runtime_unregisters_endpoint_after_terminal_run():
    runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    run_id = uuid4()
    runtime.register_run_endpoint(run_id, "http://runner")
    received = []

    async def sink(event):
        received.append(event)

    result = await runtime.run(
        {
            "run_id": str(run_id),
            "conversation_id": str(uuid4()),
            "session_id": str(uuid4()),
            "container_id": "container-1",
            "lease_epoch": 1,
            "correlation_id": "corr-terminal",
            "context_bundle": {"task": "complete"},
        },
        sink,
    )
    assert result["status"] == "COMPLETED"
    assert run_id not in runtime._run_urls


@pytest.mark.asyncio
async def test_core_runtime_keeps_endpoint_while_waiting_and_unregisters_after_input():
    runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    run_id = uuid4()
    runtime.register_run_endpoint(run_id, "http://runner")
    received = []

    async def sink(event):
        received.append(event)

    result = await runtime.run(
        {
            "run_id": str(run_id),
            "conversation_id": str(uuid4()),
            "session_id": str(uuid4()),
            "container_id": "container-1",
            "lease_epoch": 1,
            "correlation_id": "corr-waiting",
            "context_bundle": {"task": "ask: continue?"},
        },
        sink,
    )
    assert result["status"] == "WAITING_INPUT"
    assert run_id in runtime._run_urls
    interaction_id = result["events"][-1]["payload"]["interaction_id"]
    accepted = await runtime.accept_input(run_id, interaction_id, "continue")
    assert accepted["status"] == "COMPLETED"
    assert run_id not in runtime._run_urls
