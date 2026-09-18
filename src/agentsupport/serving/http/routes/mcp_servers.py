"""MCP server registration API: register, discover, update and remove servers."""

from fastapi import APIRouter, Request

from ..dependencies import agentsupport_service
from ..schemas import McpServerCreate, McpServerUpdate

router = APIRouter()


@router.post("/mcp-servers", status_code=201)
def create_mcp_server(request: Request, body: McpServerCreate):
    return agentsupport_service(request).create_mcp_server(
        server_id=body.server_id,
        name=body.name,
        transport=body.transport,
        http_url=body.http_url,
        sse_url=body.sse_url,
        headers=body.headers,
        command=body.command,
        args=body.args,
        env=body.env,
        cwd=body.cwd,
        description=body.description,
        enabled=body.enabled,
    ).model_dump(mode="json")


@router.get("/mcp-servers")
def list_mcp_servers(request: Request):
    return [
        item.model_dump(mode="json")
        for item in agentsupport_service(request).list_mcp_servers()
    ]


@router.get("/mcp-servers/{server_id}")
def get_mcp_server(request: Request, server_id: str):
    return agentsupport_service(request).get_mcp_server(server_id).model_dump(mode="json")


@router.patch("/mcp-servers/{server_id}")
def update_mcp_server(request: Request, server_id: str, body: McpServerUpdate):
    return agentsupport_service(request).update_mcp_server(
        server_id,
        name=body.name,
        http_url=body.http_url,
        sse_url=body.sse_url,
        headers=body.headers,
        command=body.command,
        args=body.args,
        env=body.env,
        cwd=body.cwd,
        description=body.description,
        enabled=body.enabled,
    ).model_dump(mode="json")


@router.delete("/mcp-servers/{server_id}", status_code=204)
def delete_mcp_server(request: Request, server_id: str) -> None:
    agentsupport_service(request).delete_mcp_server(server_id)
