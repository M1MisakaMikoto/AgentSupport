"""Contract: skill generation API surface and tenant isolation."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from _support import make_temporal_service as _make_service

SKILL_MARKDOWN = """---
name: fix-n-plus-one
description: 修复 SQLAlchemy 列表查询 N+1 的标准路径
---

# 目标

消除列表接口的 N+1 查询。
"""


@pytest.fixture
def env(tmp_path):
    return _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
        runner_result={"skill_markdown": SKILL_MARKDOWN},
    )


@pytest.fixture
def client(env):
    app = create_app(env.service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _session(env, *, tenant_id):
    service = env.service
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id, tenant_id=tenant_id)
    conversation = await service.create_conversation(session.id, "business task")
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=20)
    return session


async def _generation_completed(client, session, generation_id, *, tenant_id):
    for _ in range(50):
        response = await client.get(
            f"/sessions/{session.id}/skills/generations/{generation_id}",
            headers={"X-Tenant-Id": tenant_id},
        )
        assert response.status_code == 200
        if response.json()["status"] in {"completed", "failed"}:
            return response.json()
        await asyncio.sleep(0.05)
    raise AssertionError("generation never reached a terminal status")


async def test_generate_endpoint_completes_and_draft_is_visible(client, env):
    session = await _session(env, tenant_id="t-1")
    response = await client.post(
        f"/sessions/{session.id}/skills/generate",
        headers={"X-Tenant-Id": "t-1"},
    )
    assert response.status_code == 201
    body = response.json()
    generation = await _generation_completed(
        client, session, body["id"], tenant_id="t-1"
    )
    assert generation["status"] == "completed"
    assert generation["session_id"] == str(session.id)

    drafts = await client.get("/skill-drafts", params={"tenant_id": "t-1"})
    assert drafts.status_code == 200
    assert len(drafts.json()) == 1
    assert drafts.json()[0]["skill_id"] == "fix-n-plus-one"


async def test_generate_endpoint_rejects_foreign_tenant(client, env):
    session = await _session(env, tenant_id="t-1")
    response = await client.post(
        f"/sessions/{session.id}/skills/generate",
        headers={"X-Tenant-Id": "t-2"},
    )
    assert response.status_code == 404


async def test_generation_status_requires_session_scope(client, env):
    session = await _session(env, tenant_id="t-1")
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
    assert ok.json()["status"] in {"starting", "running", "completed", "failed"}

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
