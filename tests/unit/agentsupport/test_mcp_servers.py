"""MCP server registration, validation and dynamic activation tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.domain import PresetResources, ProjectConfig
from _support import make_temporal_service as _make_service


def _service(tmp_path):
    return _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
    ).service


def test_mcp_server_crud_and_validation(tmp_path):
    service = _service(tmp_path)
    server = service.create_mcp_server(
        server_id="gitlab",
        name="GitLab MCP",
        transport="http",
        http_url="http://mcp-gitlab:8000/mcp",
        headers={"Authorization": "$GITLAB_TOKEN"},
    )
    assert server.enabled is True
    assert service.list_mcp_servers()[0].server_id == "gitlab"
    assert service.get_mcp_server("gitlab").http_url == "http://mcp-gitlab:8000/mcp"

    updated = service.update_mcp_server("gitlab", enabled=False, name="GitLab v2")
    assert updated.enabled is False
    assert updated.name == "GitLab v2"

    service.delete_mcp_server("gitlab")
    with pytest.raises(ServiceError) as exc:
        service.get_mcp_server("gitlab")
    assert exc.value.code == "MCP_SERVER_NOT_FOUND"

    with pytest.raises(ServiceError) as exc:
        service.create_mcp_server(server_id="bad/id", name="x", transport="http")
    assert exc.value.code == "MCP_SERVER_INVALID"
    with pytest.raises(ServiceError) as exc:
        service.create_mcp_server(server_id="sse1", name="x", transport="sse")
    assert "requires sse_url" in exc.value.message
    with pytest.raises(ServiceError) as exc:
        service.create_mcp_server(
            server_id="ws1", name="x", transport="websocket", http_url="http://x"
        )
    assert exc.value.code == "MCP_TRANSPORT_UNSUPPORTED"


def test_stdio_mcp_server_round_trip(tmp_path):
    """stdio 注册：command/args/env/cwd 要能存、能读、能在会话解析里带出来。"""
    service = _service(tmp_path)
    server = service.create_mcp_server(
        server_id="document-assistant",
        name="Document-Assistant 文档工具",
        transport="stdio",
        command="python",
        args=["/app/da-mcp-server/main.py", "--transport", "stdio"],
        env={"DOC_HOST_TOKEN": "$DOC_HOST_TOKEN"},
        cwd="/app/da-mcp-server",
    )
    assert server.endpoint() == "python"
    assert service.get_mcp_server("document-assistant").args[1] == "--transport"

    workspace = service.create_workspace("ws-stdio")
    session = service.create_session(
        workspace.id,
        config=ProjectConfig(
            resources=PresetResources(mcp_refs=[{"server_id": "document-assistant"}]),
        ),
    )
    resolved = service._resolve_mcp_refs([{"server_id": "document-assistant"}], session)
    assert resolved[0]["transport"] == "stdio"
    assert resolved[0]["command"] == "python"
    assert resolved[0]["env"] == {"DOC_HOST_TOKEN": "$DOC_HOST_TOKEN"}
    assert resolved[0]["cwd"] == "/app/da-mcp-server"

    with pytest.raises(ServiceError) as exc:
        service.create_mcp_server(server_id="no-cmd", name="x", transport="stdio")
    assert "requires command" in exc.value.message


def test_session_create_validates_mcp_refs(tmp_path):
    service = _service(tmp_path)
    service.create_mcp_server(
        server_id="gitlab",
        name="GitLab MCP",
        transport="http",
        http_url="http://mcp-gitlab:8000/mcp",
    )
    workspace = service.create_workspace("ws")
    session = service.create_session(
        workspace.id,
        config=ProjectConfig(
            resources=PresetResources(mcp_refs=[{"server_id": "gitlab"}]),
        ),
    )
    assert session.id is not None

    with pytest.raises(ServiceError) as exc:
        service.create_session(
            workspace.id,
            config=ProjectConfig(resources=PresetResources(mcp_refs=[{"server_id": "nope"}])),
        )
    assert exc.value.code == "MCP_SERVER_NOT_FOUND"

    service.update_mcp_server("gitlab", enabled=False)
    with pytest.raises(ServiceError) as exc:
        service.create_session(
            workspace.id,
            config=ProjectConfig(
                resources=PresetResources(mcp_refs=[{"server_id": "gitlab"}])
            ),
        )
    assert exc.value.code == "MCP_SERVER_DISABLED"


@pytest.mark.asyncio
async def test_conversation_mcp_refs_override_inherit_and_validate(tmp_path):
    service = _service(tmp_path)
    service.create_mcp_server(
        server_id="gitlab",
        name="GitLab MCP",
        transport="http",
        http_url="http://mcp-gitlab:8000/mcp",
    )
    service.create_mcp_server(
        server_id="jira",
        name="Jira MCP",
        transport="sse",
        sse_url="http://mcp-jira:8000/sse",
    )
    workspace = service.create_workspace("ws")
    session = service.create_session(
        workspace.id,
        config=ProjectConfig(
            resources=PresetResources(mcp_refs=[{"server_id": "gitlab"}]),
        ),
    )

    inherited = await service.create_conversation(session.id, "inherit")
    assert service._resolve_mcp_refs(inherited.mcp_refs, session)[0]["server_id"] == "gitlab"

    overridden = await service.create_conversation(
        session.id, "override", mcp_refs=[{"server_id": "jira"}]
    )
    assert service._resolve_mcp_refs(overridden.mcp_refs, session)[0]["server_id"] == "jira"

    disabled = await service.create_conversation(session.id, "none", mcp_refs=[])
    assert service._resolve_mcp_refs(disabled.mcp_refs, session) == []

    with pytest.raises(ServiceError) as exc:
        await service.create_conversation(
            session.id, "missing", mcp_refs=[{"server_id": "nope"}]
        )
    assert exc.value.code == "MCP_SERVER_NOT_FOUND"


@pytest.mark.asyncio
async def test_mcp_servers_api_crud(tmp_path):
    service = _service(tmp_path)
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/mcp-servers",
            json={
                "server_id": "gitlab",
                "name": "GitLab MCP",
                "transport": "http",
                "http_url": "http://mcp-gitlab:8000/mcp",
                "headers": {"Authorization": "$GITLAB_TOKEN"},
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["server_id"] == "gitlab"

        listed = await client.get("/mcp-servers")
        assert [item["server_id"] for item in listed.json()] == ["gitlab"]

        patched = await client.patch(
            "/mcp-servers/gitlab", json={"enabled": False, "name": "GitLab v2"}
        )
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False

        removed = await client.delete("/mcp-servers/gitlab")
        assert removed.status_code == 204
        gone = await client.get("/mcp-servers/gitlab")
        assert gone.status_code == 404
