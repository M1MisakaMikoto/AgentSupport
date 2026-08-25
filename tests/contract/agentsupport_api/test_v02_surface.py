"""v0.2 contract: business routes are gone and session SSE is available."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService


@pytest.fixture
def service(tmp_path):
    return AgentSupportService(Settings(workspace_root=tmp_path))


@pytest.mark.asyncio
async def test_removed_business_routes_return_404(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in (
            "/organizations",
            "/users",
            "/presets",
            "/projects",
            "/users/00000000-0000-0000-0000-000000000001/projects",
            "/projects/00000000-0000-0000-0000-000000000001/preset",
        ):
            response = await client.get(path)
            assert response.status_code == 404, path


@pytest.mark.asyncio
async def test_session_sse_streams_events(service):
    workspace = service.create_workspace("sse")
    session = service.create_session(
        workspace.id, tenant_id="t-1", user_id="u-1", project_id="p-1"
    )
    await service.create_conversation(session.id, "hello")

    stream = service.stream_session_events(session.id)
    first = await asyncio.wait_for(anext(stream), timeout=5)
    assert first.type in {"run.started", "run.running"}
    assert first.tenant_id == "t-1"
    assert first.user_id == "u-1"
    assert first.project_id == "p-1"
    await stream.aclose()


@pytest.mark.asyncio
async def test_oversized_request_bodies_rejected(service):
    workspace = service.create_workspace("size-limit")
    session = service.create_session(workspace.id)
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        oversized_task = await client.post(
            f"/sessions/{session.id}/conversations",
            json={"task": "x" * 100_001},
        )
        assert oversized_task.status_code == 422
        oversized_metadata = await client.post(
            "/sessions",
            json={
                "workspace_id": str(workspace.id),
                "metadata": {"blob": "y" * 40_000},
            },
        )
        assert oversized_metadata.status_code == 422


@pytest.mark.asyncio
async def test_list_sessions_pagination(service):
    workspace = service.create_workspace("pagination")
    for _ in range(5):
        service.create_session(workspace.id)
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_page = await client.get("/sessions", params={"limit": 2, "offset": 0})
        second_page = await client.get("/sessions", params={"limit": 2, "offset": 2})
        assert first_page.status_code == 200
        assert len(first_page.json()) == 2
        assert len(second_page.json()) == 2
        first_ids = {item["id"] for item in first_page.json()}
        second_ids = {item["id"] for item in second_page.json()}
        assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
async def test_list_default_limit_applied(tmp_path):
    capped = AgentSupportService(
        Settings(workspace_root=tmp_path, list_default_limit=2)
    )
    workspace = capped.create_workspace("cap")
    for _ in range(3):
        capped.create_session(workspace.id)
    app = create_app(capped)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions")
        assert response.status_code == 200
        assert len(response.json()) == 2
