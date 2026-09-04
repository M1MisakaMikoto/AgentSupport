"""Contract: the AgentSupport public API intentionally ships without
token-based authentication. These tests guard that property so an auth
dependency cannot be added silently."""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from agentsupport.api import create_app
from agentsupport.config import Settings
from _support import make_temporal_service as _make_service


@pytest.fixture
def service(tmp_path):
    return _make_service(tmp_path).service


def test_openapi_declares_no_security(service):
    app = create_app(service)
    spec = app.openapi()

    assert not spec.get("components", {}).get("securitySchemes", {})
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "patch", "delete", "put"}:
                continue
            assert not operation.get("security"), (
                f"{method.upper()} {path} declares a security requirement"
            )


@pytest.mark.asyncio
async def test_public_endpoints_work_without_any_authorization(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/live", "/ready", "/metrics", "/cores"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.headers.get("www-authenticate") is None, path

        created = await client.post("/workspaces", json={"name": "no-auth"})
        assert created.status_code == 201

        invalid = await client.post(
            "/sessions", json={"workspace_id": "not-a-uuid"}
        )
        assert invalid.status_code == 422
        assert invalid.status_code != 401


def test_auth_mode_is_explicitly_none(tmp_path):
    assert Settings(workspace_root=tmp_path).api_auth_mode == "none"
    with pytest.raises(ValidationError):
        Settings(workspace_root=tmp_path, api_auth_mode="token")
