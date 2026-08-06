from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.bootstrap.container import build_agentsupport_service
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService


@pytest.fixture
def service(tmp_path):
    return AgentSupportService(
        Settings(workspace_root=tmp_path / "workspaces", enabled_skills="review")
    )


def _stub_session_id() -> str:
    return "00000000-0000-0000-0000-0000000000ab"


def test_auto_create_defaults_to_enabled(tmp_path):
    settings = Settings(workspace_root=tmp_path)
    assert settings.auto_create_missing is True
    assert settings.auto_create_scopes_set == {"all"}
    assert settings.api_auth_mode == "none"


@pytest.mark.asyncio
async def test_conversation_auto_creates_session_and_default_workspace(service):
    session_id = uuid4()
    auto_created: dict = {}
    conversation = await service.create_conversation(
        session_id, "run without setup", auto_created=auto_created
    )

    assert conversation.session_id == session_id
    session = service.get_session(session_id)
    assert session.workspace_id == service._default_workspace_id()
    assert auto_created["session"]["id"] == str(session_id)
    assert auto_created["workspace"]["id"] == str(session.workspace_id)


@pytest.mark.asyncio
async def test_conversation_auto_create_honors_workspace_hint(service):
    workspace_id = uuid4()
    auto_created: dict = {}
    conversation = await service.create_conversation(
        uuid4(), "task", workspace_id=workspace_id, auto_created=auto_created
    )

    session = service.get_session(conversation.session_id)
    assert session.workspace_id == workspace_id
    assert auto_created["workspace"]["id"] == str(workspace_id)


@pytest.mark.asyncio
async def test_conversation_auto_create_honors_project_hint(service):
    project_id = uuid4()
    auto_created: dict = {}
    conversation = await service.create_conversation(
        uuid4(), "task", project_id=project_id, auto_created=auto_created
    )

    session = service.get_session(conversation.session_id)
    project = service.get_project(project_id)
    assert session.project_id == project_id
    assert session.workspace_id == project.workspace_id
    assert auto_created["project"]["id"] == str(project_id)
    assert auto_created["user"]["id"] == str(service._default_user_id())


def test_session_auto_creates_workspace(service):
    workspace_id = uuid4()
    auto_created: dict = {}
    session = service.create_session(
        workspace_id, auto_created=auto_created, name="demo"
    )

    assert session.workspace_id == workspace_id
    assert service.workspaces[workspace_id].name == "demo"
    assert auto_created["workspace"]["id"] == str(workspace_id)
    assert auto_created["session"]["id"] == str(session.id)


def test_project_auto_creates_user_and_falls_back_preset(service):
    user_id = uuid4()
    missing_preset = uuid4()
    auto_created: dict = {}
    project = service.create_project(
        "shop", user_id, preset_id=missing_preset, auto_created=auto_created
    )

    assert project.user_id == user_id
    assert service.get_user(user_id).username == f"auto-{str(user_id)[:8]}"
    assert project.preset_id is None
    assert auto_created["user"]["id"] == str(user_id)
    assert auto_created["preset_fallback"] == "default"


def test_preset_auto_creates_user(service):
    user_id = uuid4()
    auto_created: dict = {}
    preset = service.create_preset(
        user_id, "review", auto_created=auto_created
    )

    assert preset.user_id == user_id
    assert service.get_user(user_id).id == user_id
    assert auto_created["user"]["id"] == str(user_id)


def test_user_auto_creates_organization(service):
    organization_id = uuid4()
    auto_created: dict = {}
    user = service.create_user(
        "alice", organization_id=organization_id, auto_created=auto_created
    )

    assert user.organization_id == organization_id
    assert service.get_organization(organization_id).id == organization_id
    assert auto_created["organization"]["id"] == str(organization_id)


def test_project_session_auto_creates_chain(service):
    project_id = uuid4()
    auto_created: dict = {}
    session = service.create_project_session(project_id, auto_created=auto_created)

    project = service.get_project(project_id)
    assert session.project_id == project_id
    assert session.workspace_id == project.workspace_id
    assert auto_created["project"]["id"] == str(project_id)
    assert auto_created["session"]["id"] == str(session.id)
    assert auto_created["user"]["id"] == str(service._default_user_id())


def test_auto_create_disabled_keeps_404(service):
    service.config.auto_create_missing = False
    with pytest.raises(Exception) as missing_workspace:
        service.create_session(uuid4())
    assert missing_workspace.value.code == "WORKSPACE_NOT_FOUND"

    with pytest.raises(Exception) as missing_user:
        service.create_preset(uuid4(), "review")
    assert missing_user.value.code == "USER_NOT_FOUND"


def test_scopes_can_restrict_auto_creation(service):
    service.config.auto_create_scopes = "session"
    with pytest.raises(Exception) as missing_workspace:
        service.create_session(uuid4())
    assert missing_workspace.value.code == "WORKSPACE_NOT_FOUND"

    service.config.auto_create_scopes = "session,workspace"
    session = service.create_session(uuid4())
    assert session.workspace_id is not None


def test_repeated_auto_create_converges_on_same_entities(service):
    session_id = uuid4()
    workspace_id = uuid4()
    first = service.create_session(
        workspace_id, session_id=session_id, auto_created={}
    )
    second = service.create_session(
        workspace_id, session_id=session_id, auto_created={}
    )

    assert first.id == second.id == session_id
    assert len(service.workspaces) == 1
    assert len(service.sessions) == 1


@pytest.mark.asyncio
async def test_parent_conversation_missing_still_404(service):
    session = service.create_session(uuid4())
    with pytest.raises(Exception) as missing_parent:
        await service.create_conversation(
            session.id, "task", parent_conversation_id=uuid4()
        )
    assert missing_parent.value.code == "PARENT_NOT_FOUND"


def test_reads_do_not_auto_create(service):
    with pytest.raises(Exception) as missing_session:
        service.get_session(uuid4())
    assert missing_session.value.code == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_http_conversation_auto_create_returns_auto_created(service):
    app = create_app(service)
    session_id = "00000000-0000-0000-0000-0000000000cd"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/sessions/{session_id}/conversations", json={"task": "hello"}
        )

    assert response.status_code == 201
    body = response.json()
    assert body["session_id"] == session_id
    assert body["auto_created"]["session"]["id"] == session_id
    assert "workspace" in body["auto_created"]


@pytest.mark.asyncio
async def test_http_project_auto_create_with_missing_user(tmp_path):
    service = build_agentsupport_service(
        Settings(
            workspace_root=tmp_path / "ws",
            persistence_mode="postgres",
            database_url=f"sqlite:///{tmp_path / 'auto.db'}",
        )
    )
    app = create_app(service)
    user_id = "00000000-0000-0000-0000-0000000000ef"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/projects", json={"user_id": user_id, "name": "auto-shop"}
        )

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == user_id
    assert body["auto_created"]["user"]["id"] == user_id
    assert body["auto_created"]["project"]["id"] == body["id"]
    assert "workspace" in body["auto_created"]
    assert str(service.get_user(UUID(user_id)).id) == user_id
