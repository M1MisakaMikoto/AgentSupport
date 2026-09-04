"""Per-conversation skill activation tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.domain import PresetSkill, ProjectConfig
from _support import make_temporal_service as _make_service


@pytest.fixture
def service(tmp_path):
    service = _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
        enabled_skills="debug",
    ).service
    service.create_skill("review", filename="SKILL.md", payload=b"# Review\n")
    service.create_skill("debug", filename="SKILL.md", payload=b"# Debug\n")
    return service


@pytest.mark.asyncio
async def test_conversation_skills_override_inherit_and_disable(service):
    workspace = service.create_workspace("ws")
    session = service.create_session(
        workspace.id,
        config=ProjectConfig(skills=[PresetSkill(skill_id="debug", enabled=True)]),
    )

    inherited = await service.create_conversation(session.id, "inherit")
    assert service._skills_for_conversation(inherited, session) == ["debug"]

    overridden = await service.create_conversation(
        session.id, "override", skills=[PresetSkill(skill_id="review", enabled=True)]
    )
    assert service._skills_for_conversation(overridden, session) == ["review"]

    disabled = await service.create_conversation(session.id, "none", skills=[])
    assert service._skills_for_conversation(disabled, session) == []

    with pytest.raises(ServiceError) as exc:
        await service.create_conversation(
            session.id,
            "missing",
            skills=[PresetSkill(skill_id="missing", enabled=True)],
        )
    assert exc.value.code == "SKILL_NOT_FOUND"
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_conversation_skills_persist_and_round_trip(tmp_path):
    service = _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
    ).service
    service.create_skill("review", filename="SKILL.md", payload=b"# Review\n")
    workspace = service.create_workspace("ws")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(
        session.id, "task", skills=[PresetSkill(skill_id="review", enabled=True)]
    )
    loaded = service.repository.get_conversation(conversation.id)
    assert loaded is not None
    assert [skill.skill_id for skill in loaded.skills or []] == ["review"]


@pytest.mark.asyncio
async def test_conversation_skills_api_passthrough(service):
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
        assert created.json()["skills"][0]["skill_id"] == "review"

        fetched = await client.get(f"/conversations/{created.json()['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["skills"][0]["skill_id"] == "review"

        missing = await client.post(
            f"/sessions/{session.id}/conversations",
            json={"task": "bad", "skills": [{"skill_id": "nope", "enabled": True}]},
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "SKILL_NOT_FOUND"
