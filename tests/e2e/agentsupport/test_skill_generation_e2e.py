"""E2E: generate -> review -> publish on the temporal-only core."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.skills import LocalSkillProvider
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
    return AsyncClient(transport=ASGITransport(app=create_app(env.service)), base_url="http://test")


async def _wait_generation(client, session_id, generation_id, *, tenant_id):
    for _ in range(60):
        response = await client.get(
            f"/sessions/{session_id}/skills/generations/{generation_id}",
            headers={"X-Tenant-Id": tenant_id},
        )
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"completed", "failed"}:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("generation never reached a terminal status")


@pytest.mark.asyncio
async def test_generate_review_publish(client, env, tmp_path):
    async with client:
        workspace = await client.post("/workspaces", json={"name": "demo"})
        assert workspace.status_code == 201
        workspace_id = workspace.json()["id"]

        source = (
            await client.post(
                "/sessions",
                json={"workspace_id": workspace_id, "tenant_id": "t-1"},
            )
        ).json()
        business = await client.post(
            f"/sessions/{source['id']}/conversations",
            json={"task": "business task"},
            headers={"X-Tenant-Id": "t-1"},
        )
        assert business.status_code == 201

        generated = await client.post(
            f"/sessions/{source['id']}/skills/generate",
            headers={"X-Tenant-Id": "t-1"},
        )
        assert generated.status_code == 201
        generation = await _wait_generation(
            client, source["id"], generated.json()["id"], tenant_id="t-1"
        )
        assert generation["status"] == "completed"

        drafts = await client.get("/skill-drafts", params={"tenant_id": "t-1"})
        assert drafts.status_code == 200
        draft = drafts.json()[0]
        assert draft["skill_id"] == "fix-n-plus-one"

        reviewed = await client.post(
            f"/skill-drafts/{draft['id']}/review",
            json={"decision": "approve", "note": "ok"},
            headers={"X-Tenant-Id": "t-1"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "published"

        provider = LocalSkillProvider(tmp_path / "skills")
        tenant_skills = provider.list_skills(tenant_id="t-1")
        assert [item["skill_id"] for item in tenant_skills] == ["fix-n-plus-one"]
        assert (tmp_path / "skills" / "tenants" / "t-1" / "fix-n-plus-one" / "SKILL.md").is_file()
