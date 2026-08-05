from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.adapters.persistence.sqlalchemy.repository import (
    PostgresRepository,
    RepositoryConflict,
)
from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.domain import (
    PresetDefinition,
    PresetPermissions,
    PresetResources,
    PresetSkill,
    PresetToolPolicy,
    project_config_from_definition,
)
from agentsupport.services import AgentSupportService


@pytest.fixture
def service(tmp_path):
    return AgentSupportService(
        Settings(workspace_root=tmp_path / "workspaces", enabled_skills="review,debug")
    )


def _preset_definition(skill_id: str = "review") -> PresetDefinition:
    return PresetDefinition(
        skills=[PresetSkill(skill_id=skill_id, enabled=True)],
        tool_policy=PresetToolPolicy(
            allowed_tools=["bash", "task_done"],
            approval_required_tools=["bash"],
        ),
        resources=PresetResources(env={"FEATURE": "on"}),
        permissions=PresetPermissions(max_active_sessions=3),
    )


def test_user_preset_project_flow_and_snapshot_semantics(service):
    user = service.create_user("alice")
    preset = service.create_preset(
        user.id, "code review", "standard preset", _preset_definition()
    )

    project = service.create_project("shop", user.id, preset_id=preset.id)

    assert project.user_id == user.id
    assert project.preset_id == preset.id
    assert project.config.enabled_skill_ids() == ["review"]
    assert project.config.tool_policy.allowed_tools == ["bash", "task_done"]
    assert project.config.resources.env == {"FEATURE": "on"}
    assert project.config.permissions.max_active_sessions == 3

    # Snapshot semantics: changing the preset must not affect the project.
    updated = service.update_preset(
        preset.id,
        definition=PresetDefinition(
            skills=[PresetSkill(skill_id="debug", enabled=True)],
            tool_policy=PresetToolPolicy(allowed_tools=["task_done"]),
        ),
    )
    assert updated.definition.skills[0].skill_id == "debug"
    assert service.get_project(project.id).config.enabled_skill_ids() == ["review"]


def test_project_without_preset_uses_deployment_defaults(service):
    user = service.create_user("bob")
    project = service.create_project("plain", user.id)
    assert project.preset_id is None
    assert project.config.enabled_skill_ids() == ["review", "debug"]
    assert project.config.tool_policy.allowed_tools == [
        "bash",
        "str_replace_based_edit_tool",
        "json_edit_tool",
        "sequentialthinking",
        "task_done",
    ]


def test_preset_ownership_and_disabled_guards(service):
    alice = service.create_user("alice")
    bob = service.create_user("bob")
    preset = service.create_preset(alice.id, "private", definition=_preset_definition())

    with pytest.raises(Exception) as not_owned:
        service.create_project("steal", bob.id, preset_id=preset.id)
    assert not_owned.value.code == "PRESET_NOT_OWNED"

    disabled = service.update_preset(
        preset.id, definition=PresetDefinition(enabled=False)
    )
    assert disabled.definition.enabled is False
    with pytest.raises(Exception) as disabled_error:
        service.create_project("blocked", alice.id, preset_id=preset.id)
    assert disabled_error.value.code == "PRESET_DISABLED"


@pytest.mark.asyncio
async def test_project_session_uses_project_skills_and_tool_policy(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    service = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=skills_root,
            enabled_skills="debug",
        )
    )
    user = service.create_user("alice")
    preset = service.create_preset(
        user.id, "review preset", definition=_preset_definition()
    )
    project = service.create_project("shop", user.id, preset_id=preset.id)

    session = service.create_project_session(project.id)
    assert session.project_id == project.id

    # Project config drives the tool policy used for the session.
    policy = service._tool_policy_for_session(session)
    assert policy["allowed_tools"] == ["bash", "task_done"]
    assert policy["approval_required_tools"] == ["bash"]

    # Project skills are mounted, not the deployment-global ones.
    await_session = service.create_project_session(project.id)
    await service.create_conversation(await_session.id, "run")
    inspection = await service.runtime_driver.inspect(await_session.active_container_id)
    assert inspection["read_only_mounts"] == [
        (str(skill.resolve()), "/opt/agent-skills/review")
    ]

    # Legacy sessions without a project keep the global defaults.
    workspace = service.create_workspace("legacy")
    legacy = service.create_session(workspace.id)
    assert legacy.project_id is None
    assert service._tool_policy_for_session(legacy)["allowed_tools"] == [
        "bash",
        "str_replace_based_edit_tool",
        "json_edit_tool",
        "sequentialthinking",
        "task_done",
    ]


def test_import_preset_replaces_project_config(service):
    user = service.create_user("alice")
    first = service.create_preset(
        user.id, "v1", definition=_preset_definition("review")
    )
    second = service.create_preset(
        user.id, "v2", definition=_preset_definition("debug")
    )
    project = service.create_project("shop", user.id, preset_id=first.id)

    updated, previous = service.import_preset_to_project(project.id, second.id)

    assert previous.enabled_skill_ids() == ["review"]
    assert updated.preset_id == second.id
    assert updated.config.enabled_skill_ids() == ["debug"]
    assert service.get_project(project.id).config.enabled_skill_ids() == ["debug"]


def test_preset_update_delete_and_list(service):
    user = service.create_user("alice")
    preset = service.create_preset(user.id, "default", definition=_preset_definition())
    assert len(service.list_presets(user.id)) == 1

    renamed = service.update_preset(preset.id, name="renamed")
    assert renamed.name == "renamed"

    service.delete_preset(preset.id)
    assert service.list_presets(user.id) == []
    with pytest.raises(Exception) as missing:
        service.get_preset(preset.id)
    assert missing.value.code == "PRESET_NOT_FOUND"


@pytest.mark.asyncio
async def test_projects_and_presets_api(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user = await client.post("/users", json={"username": "alice"})
        assert user.status_code == 201
        user_id = user.json()["id"]

        preset = await client.post(
            "/presets",
            json={
                "user_id": user_id,
                "name": "code review",
                "definition": {
                    "skills": [{"skill_id": "review", "enabled": True}],
                    "tools": {
                        "allowed_tools": ["bash", "task_done"],
                        "approval_required_tools": ["bash"],
                    },
                },
            },
        )
        assert preset.status_code == 201
        preset_id = preset.json()["id"]

        listed = await client.get(f"/users/{user_id}/presets")
        assert [item["id"] for item in listed.json()] == [preset_id]

        project = await client.post(
            "/projects",
            json={"user_id": user_id, "name": "shop", "preset_id": preset_id},
        )
        assert project.status_code == 201
        project_id = project.json()["id"]
        assert project.json()["config"]["skills"] == [
            {"skill_id": "review", "enabled": True}
        ]

        projects = await client.get(f"/users/{user_id}/projects")
        assert [item["id"] for item in projects.json()] == [project_id]

        session = await client.post(f"/projects/{project_id}/sessions")
        assert session.status_code == 201
        assert session.json()["project_id"] == project_id

        fetched = await client.get(f"/presets/{preset_id}")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "code review"

        missing = await client.get(
            "/presets/00000000-0000-0000-0000-000000000000"
        )
        assert missing.status_code == 404


def test_repository_user_preset_project_session_flow(tmp_path):
    repo = PostgresRepository(
        f"sqlite:///{tmp_path / 'repo.db'}", create_schema=True
    )
    user = repo.create_user("alice", repo.organization_id, "hash-a", None)
    assert repo.get_user(user.id).username == "alice"

    preset = repo.create_preset(
        user.id,
        "review",
        "desc",
        _preset_definition(),
        "hash-b",
        None,
    )
    assert preset.organization_id == user.organization_id
    assert repo.list_presets(user.id)[0].id == preset.id

    project, workspace = repo.create_project_with_workspace(
        name="shop",
        user_id=user.id,
        workspace_id=uuid4(),
        workspace_root_path="/tmp/shop",
        preset_id=preset.id,
        config=project_config_from_definition(preset.definition),
        request_hash="hash-c",
        idempotency_key=None,
    )
    assert project.preset_id == preset.id
    assert project.workspace_id == workspace.id
    assert repo.list_projects(user.id)[0].id == project.id

    session = repo.create_session(workspace, "hash-d", None, project_id=project.id)
    assert session.project_id == project.id
    assert repo.get_session(session.id).project_id == project.id
    assert repo.get_session_project(session.id).id == project.id
    assert repo.list_sessions(project_id=project.id)[0].id == session.id

    renamed = repo.update_project(project.id, name="renamed")
    assert renamed.name == "renamed"
    with pytest.raises(RepositoryConflict):
        repo.delete_project(project.id)


@pytest.mark.asyncio
async def test_extended_hierarchy_and_query_apis(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        org = await client.post("/organizations", json={"name": "acme"})
        assert org.status_code == 201
        org_id = org.json()["id"]

        orgs = await client.get("/organizations")
        assert any(item["id"] == org_id for item in orgs.json())
        fetched_org = await client.get(f"/organizations/{org_id}")
        assert fetched_org.json()["name"] == "acme"

        user = await client.post(
            "/users", json={"username": "alice", "organization_id": org_id}
        )
        assert user.status_code == 201
        user_id = user.json()["id"]

        org_users = await client.get(f"/organizations/{org_id}/users")
        assert [item["id"] for item in org_users.json()] == [user_id]
        all_users = await client.get("/users")
        assert any(item["id"] == user_id for item in all_users.json())

        preset = await client.post(
            "/presets",
            json={
                "user_id": user_id,
                "name": "p",
                "definition": {
                    "skills": [{"skill_id": "review", "enabled": True}]
                },
            },
        )
        preset_id = preset.json()["id"]
        presets = await client.get("/presets", params={"user_id": user_id})
        assert [item["id"] for item in presets.json()] == [preset_id]

        project = await client.post(
            "/projects",
            json={"user_id": user_id, "name": "shop", "preset_id": preset_id},
        )
        project_id = project.json()["id"]
        projects = await client.get("/projects", params={"user_id": user_id})
        assert [item["id"] for item in projects.json()] == [project_id]

        updated = await client.patch(
            f"/projects/{project_id}",
            json={
                "name": "shop-v2",
                "config": {
                    "skills": [{"skill_id": "debug", "enabled": True}],
                },
            },
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "shop-v2"
        assert updated.json()["config"]["skills"] == [
            {"skill_id": "debug", "enabled": True}
        ]

        session = await client.post(f"/projects/{project_id}/sessions")
        session_id = session.json()["id"]
        sessions = await client.get(f"/projects/{project_id}/sessions")
        assert [item["id"] for item in sessions.json()] == [session_id]
        fetched_session = await client.get(f"/sessions/{session_id}")
        assert fetched_session.json()["project_id"] == project_id

        conversation = await client.post(
            f"/sessions/{session_id}/conversations", json={"task": "hello"}
        )
        conversation_id = conversation.json()["id"]
        convs = await client.get(f"/sessions/{session_id}/conversations")
        assert [item["id"] for item in convs.json()] == [conversation_id]
        conv = await client.get(f"/conversations/{conversation_id}")
        assert conv.json()["id"] == conversation_id

        blocked = await client.delete(f"/projects/{project_id}")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "PROJECT_HAS_SESSIONS"

        empty = await client.post(
            "/projects", json={"user_id": user_id, "name": "empty"}
        )
        empty_id = empty.json()["id"]
        deleted = await client.delete(f"/projects/{empty_id}")
        assert deleted.status_code == 204
        gone = await client.get(f"/projects/{empty_id}")
        assert gone.status_code == 404
