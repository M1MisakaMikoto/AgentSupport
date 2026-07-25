from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from session_runner.mcp import ControlledMcpProvider
from session_runner.server import create_runner_app


def run_request(run_id, server_ref):
    return {
        "run_id": str(run_id),
        "conversation_id": str(uuid4()),
        "session_id": str(uuid4()),
        "container_id": "container-1",
        "lease_epoch": 1,
        "correlation_id": "corr-mcp",
        "context_bundle": {
            "task": "controlled MCP",
            "mcp_refs": [server_ref],
            "tool_batch": {
                "calls": [
                    {
                        "call_id": "mcp-1",
                        "name": "mcp.demo.echo",
                        "arguments": {"value": "hello"},
                    }
                ]
            },
        },
    }


@pytest.mark.asyncio
async def test_controlled_mcp_tool_requires_approval_and_denies_unknown_server():
    calls = []

    async def echo(arguments):
        calls.append(arguments["value"])
        return arguments["value"]

    provider = ControlledMcpProvider(
        {("demo", "echo"): echo}, allowed_servers={"demo"}, approval_required=True
    )
    app = create_runner_app(mcp_provider=provider)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        run_id = uuid4()
        started = await client.post("/runs", json=run_request(run_id, "demo"))
        assert started.json()["status"] == "WAITING_INPUT"
        assert calls == []
        approval_id = started.json()["events"][-1]["payload"]["interaction_id"]
        approved = await client.post(
            f"/runs/{run_id}/approval",
            json={"approval_id": approval_id, "decision": "APPROVE_ONCE"},
        )
        assert approved.json()["status"] == "COMPLETED"
        assert approved.json()["events"][0]["payload"]["output"] == "hello"
        assert calls == ["hello"]

        denied = await client.post("/runs", json=run_request(uuid4(), "forbidden"))
        assert denied.json()["status"] == "FAILED"
        assert denied.json()["events"][-1]["payload"]["code"] == "TOOL_GATEWAY_ERROR"
        assert calls == ["hello"]
