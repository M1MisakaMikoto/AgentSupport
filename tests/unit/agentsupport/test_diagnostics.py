"""Unit tests for control-plane diagnostics routing."""

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService


def _service(tmp_path, *, with_runner: bool):
    service = AgentSupportService(Settings(workspace_root=tmp_path))
    if with_runner:

        class _FakeCore:
            async def model_connectivity(self):
                return {
                    "configured": True,
                    "base_url": "https://api.example.test/anthropic",
                    "tls": {
                        "verified": False,
                        "error": "certificate verify failed: EE certificate key too weak",
                        "certificate": {"subject": "CN=api.example.test", "key_bits": 1024},
                    },
                }

        service.core_runtime = _FakeCore()
    return service


@pytest.mark.asyncio
async def test_model_connectivity_returns_runner_report(tmp_path):
    app = create_app(_service(tmp_path, with_runner=True))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/diagnostics/model-connectivity")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["tls"]["certificate"]["key_bits"] == 1024


@pytest.mark.asyncio
async def test_model_connectivity_unavailable_without_runner(tmp_path):
    app = create_app(_service(tmp_path, with_runner=False))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/diagnostics/model-connectivity")

    assert response.status_code == 503
    assert response.json()["code"] == "DIAGNOSTICS_UNAVAILABLE"
