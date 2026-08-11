"""Contract: internal Runner registration endpoints are token-gated."""

import pytest
from httpx import ASGITransport, AsyncClient

from agent_runner_contracts.registration import RUNNER_CAPABILITIES
from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService


@pytest.fixture
def client(tmp_path):
    service = AgentSupportService(
        Settings(workspace_root=tmp_path, runner_token="bootstrap-secret")
    )
    app = create_app(service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_register_requires_bootstrap_token(client):
    payload = {
        "provider": "deterministic",
        "endpoint": "http://127.0.0.1:8080",
        "version": "0.1.0",
        "capabilities": list(RUNNER_CAPABILITIES),
    }
    missing = await client.post("/runners/register", json=payload)
    assert missing.status_code == 401

    wrong = await client.post(
        "/runners/register",
        json=payload,
        headers={"X-Runner-Token": "wrong"},
    )
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_registration_lifecycle_and_dynamic_cores(client):
    payload = {
        "provider": "deterministic",
        "endpoint": "http://127.0.0.1:8080",
        "version": "0.1.0",
        "capabilities": list(RUNNER_CAPABILITIES),
    }
    created = await client.post(
        "/runners/register",
        json=payload,
        headers={"X-Runner-Token": "bootstrap-secret"},
    )
    assert created.status_code == 201
    body = created.json()
    runner_id = body["runner_id"]
    runner_token = body["token"]

    cores = await client.get("/cores")
    assert cores.status_code == 200
    assert any(item["runner_id"] == runner_id for item in cores.json())

    heartbeat = await client.post(
        f"/runners/{runner_id}/heartbeat",
        json={"status": "READY", "load": 1},
        headers={"X-Runner-Token": runner_token},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["load"] == 1

    unauthorized_heartbeat = await client.post(
        f"/runners/{runner_id}/heartbeat",
        json={"status": "READY", "load": 1},
    )
    assert unauthorized_heartbeat.status_code == 401

    removed = await client.delete(
        f"/runners/{runner_id}", headers={"X-Runner-Token": runner_token}
    )
    assert removed.status_code == 204

    gone = await client.post(
        f"/runners/{runner_id}/heartbeat",
        json={"status": "READY"},
        headers={"X-Runner-Token": runner_token},
    )
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_registration_endpoints_are_not_in_openapi(client):
    app = client._transport.app
    spec = app.openapi()
    assert "/runners/register" not in spec["paths"]
    assert not spec.get("components", {}).get("securitySchemes", {})
