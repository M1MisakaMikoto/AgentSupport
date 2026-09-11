"""The candidate pool lives on the session; conversations no longer pick skills."""

from __future__ import annotations

import pytest
from _support import make_temporal_service as _make_service
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.domain import PresetSkill, ProjectConfig

SKILL_MD = "---\nname: review\ndescription: 输出审查报告\n---\n\n# Review\n".encode()


@pytest.fixture
def env(tmp_path):
    environment = _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
    )
    environment.service.create_skill("review", filename="SKILL.md", payload=SKILL_MD)
    return environment


async def test_session_pool_is_shipped_as_catalog_and_package(env):
    service = env.service
    workspace = service.create_workspace("ws")
    session = service.create_session(
        workspace.id,
        config=ProjectConfig(skills=[PresetSkill(skill_id="review", enabled=True)]),
    )

    conversation = await service.create_conversation(session.id, "task")
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=15)

    bundle = env.fake.captured[0]["context_bundle"]
    assert [item["skill_id"] for item in bundle["skill_catalog"]] == ["review"]
    assert bundle["skill_catalog"][0]["description"] == "输出审查报告"
    assert "content" not in bundle["skill_catalog"][0]
    assert bundle["skill_package"][0]["skill_id"] == "review"
    assert "skills" not in bundle


async def test_empty_pool_ships_an_empty_catalog(env):
    service = env.service
    workspace = service.create_workspace("ws")
    session = service.create_session(workspace.id)

    conversation = await service.create_conversation(session.id, "task")
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=15)

    bundle = env.fake.captured[0]["context_bundle"]
    assert bundle["skill_catalog"] == []
    assert bundle["skill_package"] == []


async def test_conversation_level_skill_picking_is_gone(env):
    service = env.service
    workspace = service.create_workspace("ws")
    session = service.create_session(workspace.id)
    app = create_app(service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/sessions/{session.id}/conversations",
            json={
                "task": "review this",
                "skills": [{"skill_id": "review", "enabled": True}],
            },
        )
        assert created.status_code == 201, created.text
        assert "skills" not in created.json()

        fetched = await client.get(f"/conversations/{created.json()['id']}")
        assert fetched.status_code == 200
        assert "skills" not in fetched.json()


def test_unknown_skill_in_the_pool_is_rejected_at_session_creation(env):
    service = env.service
    workspace = service.create_workspace("ws")

    with pytest.raises(ServiceError) as exc:
        service.create_session(
            workspace.id,
            config=ProjectConfig(skills=[PresetSkill(skill_id="missing", enabled=True)]),
        )

    assert exc.value.code == "SKILL_NOT_FOUND"
    assert exc.value.status_code == 404
