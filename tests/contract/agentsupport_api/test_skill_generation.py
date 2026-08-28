"""Contract: skill generation API surface and tenant isolation."""

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from agent_runner_contracts.events import EventEnvelope
from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService

SKILL_MARKDOWN = """---
name: fix-n-plus-one
description: 修复 SQLAlchemy 列表查询 N+1 的标准路径
---

# 目标

消除列表接口的 N+1 查询。
"""


class _CompletedCore:
    def __init__(self, result):
        self.result = result

    async def run(self, request: dict, event_sink):
        await event_sink(
            EventEnvelope(
                run_id=UUID(request["run_id"]),
                seq=1,
                type="run.completed",
                payload={"result": self.result},
                source="runner",
            )
        )
        return {"status": "RUNNING", "events": []}

    def register_run_endpoint(self, run_id, endpoint):
        return None


class _FakeRuntimeDriver:
    async def start(
        self,
        session_id,
        workspace_path,
        lease_epoch,
        workspace_id=None,
        read_only_mounts=None,
        runtime_operation_id=None,
    ):
        return f"container-{session_id}"

    async def stop(self, container_id, *, force=False):
        return True

    async def inspect(self, container_id):
        return {"read_only_mounts": []}

    async def endpoint(self, container_id):
        return None


@pytest.fixture
def service(tmp_path):
    svc = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=tmp_path / "skills",
        )
    )
    svc.core_runtime = _CompletedCore({"skill_markdown": SKILL_MARKDOWN})
    svc.runtime_driver = _FakeRuntimeDriver()
    return svc


@pytest.fixture
def client(service):
    app = create_app(service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _session(service, *, tenant_id):
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id, tenant_id=tenant_id)
    await service.create_conversation(session.id, "business task")
    return session


async def test_generate_endpoint_completes_and_draft_is_visible(client, service):
    session = await _session(service, tenant_id="t-1")
    response = await client.post(
        f"/sessions/{session.id}/skills/generate",
        headers={"X-Tenant-Id": "t-1"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["session_id"] == str(session.id)

    drafts = await client.get("/skill-drafts", params={"tenant_id": "t-1"})
    assert drafts.status_code == 200
    assert len(drafts.json()) == 1
    assert drafts.json()[0]["skill_id"] == "fix-n-plus-one"


async def test_generate_endpoint_rejects_foreign_tenant(client, service):
    session = await _session(service, tenant_id="t-1")
    response = await client.post(
        f"/sessions/{session.id}/skills/generate",
        headers={"X-Tenant-Id": "t-2"},
    )
    assert response.status_code == 404


async def test_generation_status_requires_session_scope(client, service):
    session = await _session(service, tenant_id="t-1")
    created = await client.post(
        f"/sessions/{session.id}/skills/generate",
        headers={"X-Tenant-Id": "t-1"},
    )
    generation_id = created.json()["id"]

    ok = await client.get(
        f"/sessions/{session.id}/skills/generations/{generation_id}",
        headers={"X-Tenant-Id": "t-1"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "completed"

    foreign = await client.get(
        f"/sessions/{session.id}/skills/generations/{generation_id}",
        headers={"X-Tenant-Id": "t-2"},
    )
    assert foreign.status_code == 404

    wrong_session = await client.get(
        f"/sessions/00000000-0000-0000-0000-000000000099/skills/generations/{generation_id}",
        headers={"X-Tenant-Id": "t-1"},
    )
    assert wrong_session.status_code == 404


async def test_review_approve_publishes_and_foreign_tenant_blocked(client, service):
    session = await _session(service, tenant_id="t-1")
    await client.post(
        f"/sessions/{session.id}/skills/generate",
        headers={"X-Tenant-Id": "t-1"},
    )
    drafts = await client.get("/skill-drafts", params={"tenant_id": "t-1"})
    draft_id = drafts.json()[0]["id"]

    foreign = await client.post(
        f"/skill-drafts/{draft_id}/review",
        json={"decision": "approve"},
        headers={"X-Tenant-Id": "t-2"},
    )
    assert foreign.status_code == 404

    approved = await client.post(
        f"/skill-drafts/{draft_id}/review",
        json={"decision": "approve", "note": "looks good"},
        headers={"X-Tenant-Id": "t-1"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "published"

    again = await client.post(
        f"/skill-drafts/{draft_id}/review",
        json={"decision": "reject"},
        headers={"X-Tenant-Id": "t-1"},
    )
    assert again.status_code == 409


async def test_drafts_list_is_tenant_filtered(client, service):
    first = await _session(service, tenant_id="t-1")
    second = await _session(service, tenant_id="t-2")
    await client.post(f"/sessions/{first.id}/skills/generate", headers={"X-Tenant-Id": "t-1"})
    await client.post(f"/sessions/{second.id}/skills/generate", headers={"X-Tenant-Id": "t-2"})

    t1 = await client.get("/skill-drafts", params={"tenant_id": "t-1"})
    t2 = await client.get("/skill-drafts", params={"tenant_id": "t-2"})
    assert len(t1.json()) == 1
    assert len(t2.json()) == 1
    assert t1.json()[0]["tenant_id"] == "t-1"
    assert t2.json()[0]["tenant_id"] == "t-2"


async def test_review_rejects_invalid_decision(client, service):
    session = await _session(service, tenant_id="t-1")
    await client.post(f"/sessions/{session.id}/skills/generate", headers={"X-Tenant-Id": "t-1"})
    drafts = await client.get("/skill-drafts", params={"tenant_id": "t-1"})
    draft_id = drafts.json()[0]["id"]

    response = await client.post(
        f"/skill-drafts/{draft_id}/review",
        json={"decision": "maybe"},
        headers={"X-Tenant-Id": "t-1"},
    )
    assert response.status_code == 422

