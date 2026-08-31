"""v0.2 session-scoped execution config and label behavior."""

from uuid import uuid4

import pytest

from agentsupport.config import Settings
from agentsupport.domain import PresetSkill, PresetToolPolicy, ProjectConfig
from agentsupport.services import AgentSupportService


@pytest.fixture
def service(tmp_path):
    service = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=tmp_path / "skills",
            enabled_skills="debug",
        )
    )
    service.create_skill("review", filename="SKILL.md", payload=b"# Review\n")
    return service


def _config() -> ProjectConfig:
    return ProjectConfig(
        skills=[PresetSkill(skill_id="review", enabled=True)],
        tool_policy=PresetToolPolicy(
            allowed_tools=["bash", "task_done"],
            approval_required_tools=["bash"],
        ),
    )


def test_session_config_drives_skills_and_tool_policy(service):
    session = service.create_session(uuid4(), config=_config())
    assert service._skills_for_session(session) == ["review"]
    policy = service._tool_policy_for_session(session)
    assert "bash" in policy["allowed_tools"]
    assert "bash" in policy["approval_required_tools"]


def test_session_without_config_uses_deployment_defaults(service):
    session = service.create_session(uuid4())
    assert service._skills_for_session(session) == ["debug"]
    assert service._tool_policy_for_session(session)["allowed_tools"] == [
        "bash",
        "str_replace_based_edit_tool",
        "json_edit_tool",
        "word_edit_tool",
        "excel_edit_tool",
        "pdf_tool",
        "document_convert_tool",
        "sequentialthinking",
        "task_done",
    ]


def test_labels_and_metadata_are_stored_and_filtered(service):
    session = service.create_session(
        uuid4(),
        tenant_id="t-1",
        user_id="u-1",
        project_id="p-2",
        metadata={"team": "platform"},
    )
    assert session.metadata == {"team": "platform"}

    listed = service.list_sessions(tenant_id="t-1", user_id="u-1", project_id="p-2")
    assert [item.id for item in listed] == [session.id]
    assert service.list_sessions(tenant_id="t-other") == []


@pytest.mark.asyncio
async def test_events_carry_session_labels(service):
    session = service.create_session(
        uuid4(), tenant_id="t-1", user_id="u-1", project_id="p-2"
    )
    await service.create_conversation(session.id, "task")
    conversations = service.list_conversations(session_id=session.id)
    events = service.events(conversations[0].id)
    assert all(event.tenant_id == "t-1" for event in events)
    assert all(event.user_id == "u-1" for event in events)
    assert all(event.project_id == "p-2" for event in events)


def test_repository_round_trips_config_and_labels(tmp_path):
    from agentsupport.repository import PostgresRepository

    repository = PostgresRepository(
        f"sqlite:///{tmp_path / 'session-config.db'}", create_schema=True
    )
    workspace = repository.create_workspace("demo", "/workspace/demo", "h" * 64, "w1")
    session = repository.create_session(
        workspace,
        "b" * 64,
        "s1",
        tenant_id="t-9",
        user_id="u-9",
        project_id="p-9",
        metadata={"team": "x"},
        config=_config(),
    )
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.tenant_id == "t-9"
    assert loaded.user_id == "u-9"
    assert loaded.project_id == "p-9"
    assert loaded.metadata == {"team": "x"}
    assert loaded.config is not None
    assert loaded.config.enabled_skill_ids() == ["review"]
    assert repository.list_sessions(tenant_id="t-9")[0].id == session.id
