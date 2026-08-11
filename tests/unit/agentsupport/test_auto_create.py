"""v0.2 auto-create semantics: explicit-ID create-if-missing only."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService


@pytest.fixture
def service(tmp_path):
    return AgentSupportService(
        Settings(workspace_root=tmp_path / "workspaces", enabled_skills="review")
    )


def test_auto_create_defaults_to_enabled_with_narrowed_scopes(tmp_path):
    settings = Settings(workspace_root=tmp_path)
    assert settings.auto_create_missing is True
    assert settings.auto_create_scopes_set == {"workspace", "session"}
    assert settings.api_auth_mode == "none"


def test_session_auto_creates_workspace_with_explicit_id(service):
    workspace_id = uuid4()
    auto_created: dict = {}
    session = service.create_session(
        workspace_id, auto_created=auto_created, name="demo"
    )

    assert session.workspace_id == workspace_id
    assert service.workspaces[workspace_id].name == "demo"
    assert auto_created["workspace"]["id"] == str(workspace_id)
    assert auto_created["session"]["id"] == str(session.id)


def test_repeated_explicit_id_converges(service):
    workspace_id = uuid4()
    first = service.create_session(workspace_id)
    second = service.create_session(workspace_id)
    assert first.workspace_id == second.workspace_id == workspace_id
    assert len(service.list_workspaces()) == 1


def test_auto_create_disabled_keeps_404(tmp_path):
    service = AgentSupportService(
        Settings(workspace_root=tmp_path, auto_create_missing=False)
    )
    with pytest.raises(ServiceError) as exc:
        service.create_session(uuid4())
    assert exc.value.code == "WORKSPACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_conversation_auto_creates_session_and_workspace(service):
    session_id = uuid4()
    workspace_id = uuid4()
    auto_created: dict = {}
    conversation = await service.create_conversation(
        session_id, "task", workspace_id=workspace_id, auto_created=auto_created
    )

    session = service.get_session(session_id)
    assert session.workspace_id == workspace_id
    assert conversation.session_id == session_id
    assert auto_created["session"]["id"] == str(session_id)
    assert auto_created["workspace"]["id"] == str(workspace_id)


@pytest.mark.asyncio
async def test_conversation_without_workspace_hint_keeps_404(service):
    with pytest.raises(ServiceError) as exc:
        await service.create_conversation(uuid4(), "task")
    assert exc.value.code == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_conversation_auto_create_disabled_keeps_404(tmp_path):
    service = AgentSupportService(
        Settings(workspace_root=tmp_path, auto_create_missing=False)
    )
    with pytest.raises(ServiceError) as exc:
        await service.create_conversation(uuid4(), "task", workspace_id=uuid4())
    assert exc.value.code == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_http_session_auto_create_returns_auto_created(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        workspace_id = str(uuid4())
        response = await client.post(
            "/sessions", json={"workspace_id": workspace_id, "name": "demo"}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["auto_created"]["workspace"]["id"] == workspace_id
        assert body["auto_created"]["session"]["id"] == body["id"]


@pytest.mark.asyncio
async def test_http_conversation_auto_create_returns_auto_created(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = str(uuid4())
        workspace_id = str(uuid4())
        response = await client.post(
            f"/sessions/{session_id}/conversations",
            json={"task": "hello", "workspace_id": workspace_id},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["auto_created"]["session"]["id"] == session_id
        assert body["auto_created"]["workspace"]["id"] == workspace_id


def test_labels_are_opaque_and_never_created(service):
    session = service.create_session(
        uuid4(), tenant_id="t-1", user_id="u-1", project_id="p-2"
    )
    assert session.tenant_id == "t-1"
    assert session.user_id == "u-1"
    assert session.project_id == "p-2"
