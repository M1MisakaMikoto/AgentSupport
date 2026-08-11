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
