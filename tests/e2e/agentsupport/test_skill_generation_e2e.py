"""E2E: generate -> review -> publish -> inject tenant-scoped skill."""

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

# 步骤

1. 定位循环内查询。
2. 改为批量加载。
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
    svc.core_runtime = _CompletedCore(
        {"result": "business task done"}
    )
    svc.runtime_driver = _FakeRuntimeDriver()
    return svc


@pytest.fixture
def client(service):
    return AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://test")


async def _create_session(client, workspace_id, tenant_id):
    response = await client.post(
        "/sessions",
        json={"workspace_id": workspace_id, "tenant_id": tenant_id},
    )
    assert response.status_code == 201
    return response.json()


async def test_generate_review_publish_and_inject(client):
    async with client:
        workspace = await client.post("/workspaces", json={"name": "demo"})
        assert workspace.status_code == 201
        workspace_id = workspace.json()["id"]

        source = await _create_session(client, workspace_id, "t-1")
        business = await client.post(
            f"/sessions/{source['id']}/conversations",
            json={"task": "business task"},
            headers={"X-Tenant-Id": "t-1"},
        )
        assert business.status_code == 201

        # switch the fake agent output to the generated skill contract
        svc = client._transport.app.state.service
        svc.core_runtime = _CompletedCore({"skill_markdown": SKILL_MARKDOWN})

        generated = await client.post(
            f"/sessions/{source['id']}/skills/generate",
            headers={"X-Tenant-Id": "t-1"},
        )
        assert generated.status_code == 201
        assert generated.json()["status"] == "completed"

        drafts = await client.get("/skill-drafts", params={"tenant_id": "t-1"})
        assert drafts.status_code == 200
        assert len(drafts.json()) == 1
        draft_id = drafts.json()[0]["id"]

        published = await client.post(
            f"/skill-drafts/{draft_id}/review",
            json={"decision": "approve"},
            headers={"X-Tenant-Id": "t-1"},
        )
        assert published.status_code == 200
        assert published.json()["status"] == "published"

        # the published skill is injectable in the same tenant ...
        same_tenant = await _create_session(client, workspace_id, "t-1")
        with_skill = await client.post(
            f"/sessions/{same_tenant['id']}/conversations",
            json={
                "task": "use the skill",
                "skills": [{"skill_id": "fix-n-plus-one", "enabled": True}],
            },
            headers={"X-Tenant-Id": "t-1"},
        )
        assert with_skill.status_code == 201

        # ... and invisible to a foreign tenant.
        other_tenant = await _create_session(client, workspace_id, "t-2")
        without_skill = await client.post(
            f"/sessions/{other_tenant['id']}/conversations",
            json={
                "task": "should fail",
                "skills": [{"skill_id": "fix-n-plus-one", "enabled": True}],
            },
            headers={"X-Tenant-Id": "t-2"},
        )
        assert without_skill.status_code == 404
        assert without_skill.json()["code"] == "SKILL_NOT_FOUND"
