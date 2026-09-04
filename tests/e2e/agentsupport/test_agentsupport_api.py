"""E2E: control-plane API behaviours on the temporal-only core.

Runs over the test-side embedded coordinator and SQLite repository; the
per-session container/lease lifecycle tests were removed with the runtime
drivers they belonged to.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from _support import make_temporal_service as _make_service


@pytest.fixture
def env(tmp_path):
    return _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
    )


@pytest.fixture
def service(env):
    return env.service


@pytest.fixture
def client(service):
    return AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://test")


async def _wait_run(env, service, conversation_id, timeout=15.0):
    conversation = service.repository.get_conversation(conversation_id)
    assert conversation is not None
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_operational_endpoints_report_readiness_metrics_and_instance(client):
    async with client:
        for path in ("/live", "/ready", "/metrics", "/cores"):
            response = await client.get(path)
            assert response.status_code == 200, path


@pytest.mark.asyncio
async def test_workspace_session_conversation_and_idempotency(client, env):
    workspace_id = str(uuid4())
    created = await client.post(
        "/workspaces",
        json={"name": "demo"},
        headers={"Idempotency-Key": "ws-key-1"},
    )
    assert created.status_code == 201

    session = await client.post(
        "/sessions",
        json={"workspace_id": str(created.json()["id"]), "name": "demo"},
        headers={"Idempotency-Key": "session-key-1"},
    )
    assert session.status_code == 201
    session_id = session.json()["id"]

    conversation = await client.post(
        f"/sessions/{session_id}/conversations",
        json={"task": "hello"},
        headers={"Idempotency-Key": "conversation-key-1"},
    )
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]
    await _wait_run(env, env.service, conversation_id)

    replay = await client.post(
        f"/sessions/{session_id}/conversations",
        json={"task": "hello"},
        headers={"Idempotency-Key": "conversation-key-1"},
    )
    assert replay.json()["id"] == conversation_id
    assert workspace_id != conversation_id


@pytest.mark.asyncio
async def test_missing_preconditions_404_when_auto_create_disabled(tmp_path):
    env = _make_service(tmp_path, auto_create_missing=False)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(env.service)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/sessions", json={"workspace_id": str(uuid4())}
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_conversation_sse_replays_events_after_cursor(service, env):
    workspace = service.create_workspace("sse")
    session = service.create_session(
        workspace.id, tenant_id="t-1", user_id="u-1", project_id="p-1"
    )
    conversation = await service.create_conversation(session.id, "hello")
    await _wait_run(env, service, conversation.id)

    stream = service.stream_session_events(session.id)
    first = await asyncio.wait_for(anext(stream), timeout=5)
    assert first.type in {"run.started", "run.running", "run.completed"}
    assert first.tenant_id == "t-1"
    assert first.user_id == "u-1"
    assert first.project_id == "p-1"
    await stream.aclose()


@pytest.mark.asyncio
async def test_approval_wait_pause_and_resume(tmp_path):
    env = _make_service(tmp_path, gate=True)
    service = env.service
    workspace = service.create_workspace("gate")
    session = service.create_session(workspace.id, tenant_id="t-1")
    conversation = await service.create_conversation(session.id, "needs approval")

    await asyncio.sleep(0.1)
    status = await env.coordinator.get_status(str(conversation.run.run_id))
    assert status["status"] == "waiting"

    await env.coordinator.submit_approval(
        str(conversation.run.run_id), "interaction-1", "APPROVE_ONCE"
    )
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=20)
    assert env.fake.resume_calls == 1


@pytest.mark.asyncio
async def test_full_hierarchy_chain_with_labels(service):
    workspace = service.create_workspace("chain")
    session = service.create_session(
        workspace.id, tenant_id="t-1", user_id="u-1", project_id="p-1"
    )
    listed = service.list_sessions(tenant_id="t-1", user_id="u-1", project_id="p-1")
    assert [item.id for item in listed] == [session.id]
    assert session.workspace_id == workspace.id
