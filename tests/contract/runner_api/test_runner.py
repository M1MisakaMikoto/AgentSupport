from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from session_runner.server import create_runner_app


@pytest.mark.asyncio
async def test_runner_user_input_flow():
    app = create_runner_app()
    run_id = uuid4()
    conversation_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        response = await client.post(
            "/runs",
            json={
                "run_id": str(run_id),
                "conversation_id": str(conversation_id),
                "session_id": str(uuid4()),
                "container_id": "container-1",
                "lease_epoch": 1,
                "fence_epoch": 0,
                "correlation_id": "corr-1",
                "context_bundle": {"task": "ask: what should I do?"},
            },
        )
        assert response.json()["status"] == "WAITING_INPUT"
        interaction_id = response.json()["events"][1]["payload"]["interaction_id"]
        accepted = await client.post(
            f"/runs/{run_id}/input", json={"interaction_id": interaction_id, "value": "continue"}
        )
        assert accepted.json()["status"] == "COMPLETED"
        assert [event["type"] for event in accepted.json()["events"]] == [
            "message",
            "run.completed",
        ]
        events = await client.get(f"/runs/{run_id}/events?after_seq=1")
        assert events.json()[-1]["type"] == "run.completed"


@pytest.mark.asyncio
async def test_runner_commands_are_idempotent_by_command_id():
    app = create_runner_app()
    run_id = uuid4()
    command_id = uuid4()
    request = {
        "run_id": str(run_id),
        "conversation_id": str(uuid4()),
        "session_id": str(uuid4()),
        "container_id": "container-command-idempotency",
        "lease_epoch": 1,
        "correlation_id": "command-idempotency",
        "context_bundle": {"task": "ask:once"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        waiting = (await client.post("/runs", json=request)).json()
        payload = {
            "interaction_id": waiting["events"][-1]["payload"]["interaction_id"],
            "value": "only once",
            "command_id": str(command_id),
        }

        first = (await client.post(f"/runs/{run_id}/input", json=payload)).json()
        duplicate = (await client.post(f"/runs/{run_id}/input", json=payload)).json()
        events = (await client.get(f"/runs/{run_id}/events")).json()

    assert duplicate == first
    assert [event["type"] for event in events].count("message") == 1
    assert [event["type"] for event in events].count("run.completed") == 1


@pytest.mark.asyncio
async def test_runner_duplicate_run_and_cancel_are_idempotent():
    app = create_runner_app()
    run_id = uuid4()
    payload = {
        "run_id": str(run_id),
        "conversation_id": str(uuid4()),
        "session_id": str(uuid4()),
        "container_id": "container-1",
        "lease_epoch": 1,
        "correlation_id": "corr-idempotent",
        "context_bundle": {"task": "complete"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        first = await client.post("/runs", json=payload)
        duplicate = await client.post("/runs", json=payload)
        assert duplicate.json()["status"] == first.json()["status"]
        assert [event["type"] for event in duplicate.json()["events"]] == [
            "run.started",
            "message",
            "run.completed",
        ]
        cancelled = await client.post(f"/runs/{run_id}/cancel")
        repeated = await client.post(f"/runs/{run_id}/cancel")
        assert cancelled.json()["events"][-1]["type"] == "run.cancelled"
        assert repeated.json()["events"] == []


@pytest.mark.asyncio
async def test_runner_rejects_mismatched_fence_epoch():
    app = create_runner_app()
    run_id = uuid4()
    payload = {
        "run_id": str(run_id),
        "conversation_id": str(uuid4()),
        "session_id": str(uuid4()),
        "container_id": "container-1",
        "lease_epoch": 2,
        "fence_epoch": 1,
        "correlation_id": "corr-fence",
        "context_bundle": {"task": "complete"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        response = await client.post("/runs", json=payload)
    assert response.status_code == 409
    assert response.json()["detail"] == "session fence epoch mismatch"


@pytest.mark.asyncio
async def test_runner_resume_from_checkpoint():
    app = create_runner_app()
    run_id = uuid4()
    conversation_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        started = await client.post(
            "/runs",
            json={
                "run_id": str(run_id),
                "conversation_id": str(conversation_id),
                "session_id": str(uuid4()),
                "container_id": "container-1",
                "lease_epoch": 4,
                "fence_epoch": 0,
                "correlation_id": "corr-1",
                "context_bundle": {"task": "ask: checkpoint me"},
            },
        )
        assert started.json()["status"] == "WAITING_INPUT"
        checkpoint = await client.post(f"/runs/{run_id}/checkpoint", json={"reason": "approval"})
        resumed = await client.post(
            f"/runs/{run_id}/resume",
            json={"checkpoint": checkpoint.json(), "value": "approved"},
        )
        assert resumed.json()["status"] == "COMPLETED"
        assert resumed.json()["events"][-2]["type"] == "message"


@pytest.mark.asyncio
async def test_runner_approval_flow():
    app = create_runner_app()
    run_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        started = await client.post(
            "/runs",
            json={
                "run_id": str(run_id),
                "conversation_id": str(uuid4()),
                "session_id": str(uuid4()),
                "container_id": "container-1",
                "lease_epoch": 1,
                "correlation_id": "corr-approval",
                "context_bundle": {"task": "ask: approve tool?"},
            },
        )
        approval_id = started.json()["events"][-1]["payload"]["interaction_id"]
        approved = await client.post(
            f"/runs/{run_id}/approval",
            json={"approval_id": approval_id, "decision": "APPROVE_ONCE"},
        )
        assert approved.json()["status"] == "COMPLETED"
        assert approved.json()["events"][-1]["payload"]["result"]["approval"] == "APPROVE_ONCE"


@pytest.mark.asyncio
async def test_runner_executes_tool_batch_only_after_approval():
    calls = []

    async def write_tool(arguments):
        calls.append(arguments["value"])
        return "written"

    app = create_runner_app({"workspace.write": write_tool})
    run_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        request = {
            "run_id": str(run_id),
            "conversation_id": str(uuid4()),
            "session_id": str(uuid4()),
            "container_id": "container-1",
            "lease_epoch": 1,
            "correlation_id": "corr-tool",
            "tool_policy": {
                "tools": [{"name": "workspace.write", "requires_approval": True}],
                "allowed_tools": ["workspace.write"],
            },
            "context_bundle": {
                "task": "tool run",
                "tool_batch": {
                    "calls": [
                        {
                            "call_id": "write-1",
                            "name": "workspace.write",
                            "arguments": {"value": "done"},
                        }
                    ]
                },
            },
        }
        started = await client.post("/runs", json=request)
        assert started.json()["status"] == "WAITING_INPUT"
        assert calls == []
        checkpoint = await client.post(f"/runs/{run_id}/checkpoint", json={"reason": "pause"})
        checkpoint_payload = checkpoint.json()
        assert checkpoint_payload["pending_tool_calls"][0]["call_id"] == "write-1"
        assert checkpoint_payload["tool_batch_hash"]
        assert (
            checkpoint_payload["pending_interaction"]["tool_batch"]["calls"][0]["call_id"]
            == "write-1"
        )
        approval_id = started.json()["events"][-1]["payload"]["interaction_id"]
        approved = await client.post(
            f"/runs/{run_id}/approval",
            json={"approval_id": approval_id, "decision": "APPROVE_ONCE"},
        )
        assert approved.json()["status"] == "COMPLETED"
        assert calls == ["done"]
        assert approved.json()["events"][0]["type"] == "tool.result"


@pytest.mark.asyncio
async def test_runner_restores_pending_tool_batch_in_new_process():
    calls = []

    async def write_tool(arguments):
        calls.append(arguments["value"])
        return "written"

    run_id = uuid4()
    request = {
        "run_id": str(run_id),
        "conversation_id": str(uuid4()),
        "session_id": str(uuid4()),
        "container_id": "container-1",
        "lease_epoch": 1,
        "correlation_id": "corr-tool-resume",
        "tool_policy": {
            "tools": [{"name": "workspace.write", "requires_approval": True}],
            "allowed_tools": ["workspace.write"],
        },
        "context_bundle": {
            "task": "tool run",
            "tool_batch": {
                "calls": [
                    {
                        "call_id": "write-1",
                        "name": "workspace.write",
                        "arguments": {"value": "restored"},
                    }
                ]
            },
        },
    }
    first_app = create_runner_app({"workspace.write": write_tool})
    async with AsyncClient(
        transport=ASGITransport(app=first_app), base_url="http://runner"
    ) as client:
        started = await client.post("/runs", json=request)
        assert started.json()["status"] == "WAITING_INPUT"
        checkpoint = await client.post(f"/runs/{run_id}/checkpoint", json={"reason": "pause"})

    second_app = create_runner_app({"workspace.write": write_tool})
    async with AsyncClient(
        transport=ASGITransport(app=second_app), base_url="http://runner"
    ) as client:
        resumed = await client.post(
            f"/runs/{run_id}/resume",
            json={"checkpoint": checkpoint.json(), "value": "APPROVE_ONCE"},
        )

    assert resumed.status_code == 200
    assert calls == ["restored"]
    assert [event["type"] for event in resumed.json()["events"]] == [
        "checkpoint.restored",
        "tool.result",
        "run.completed",
    ]
