"""Unit tests for the Runner registration client."""

import httpx
import pytest

from agent_runner_contracts.registration import RunnerRegistrationResponse
from session_runner.registration import (
    RunnerRegistrationClient,
    runner_registration_client_from_env,
)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_register_heartbeat_deregister(monkeypatch):
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "token": request.headers.get("X-Runner-Token"),
            }
        )
        if request.url.path == "/runners/register":
            return httpx.Response(
                201,
                json={
                    "runner_id": "11111111-1111-1111-1111-111111111111",
                    "token": "runner-token",
                },
            )
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"runner_id": "x", "status": "READY"})
        return httpx.Response(204)

    client = RunnerRegistrationClient(
        control_plane_url="http://control:8000",
        bootstrap_token="bootstrap",
        endpoint="http://runner:8080",
        provider="deterministic",
        heartbeat_seconds=1,
        transport=_mock_transport(handler),
    )

    assert await client.register() is True
    assert client.registered()
    assert client._response == RunnerRegistrationResponse(
        runner_id="11111111-1111-1111-1111-111111111111", token="runner-token"
    )
    assert await client._heartbeat_once() is True
    assert await client.deregister() is True

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://control:8000/runners/register"
    assert calls[0]["token"] == "bootstrap"
    assert calls[1]["token"] == "runner-token"
    assert calls[2]["method"] == "DELETE"


@pytest.mark.asyncio
async def test_registration_failure_is_non_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": "RUNNER_REGISTRATION_DISABLED"})

    client = RunnerRegistrationClient(
        control_plane_url="http://control:8000",
        bootstrap_token="bootstrap",
        endpoint="http://runner:8080",
        provider="deterministic",
        transport=_mock_transport(handler),
    )
    assert await client.register() is False
    assert not client.registered()
    assert await client.deregister() is False


def test_client_from_env_requires_configuration(monkeypatch):
    monkeypatch.delenv("AGENTSUPPORT_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("SESSION_RUNNER_TOKEN", raising=False)
    assert runner_registration_client_from_env(mode="deterministic") is None

    monkeypatch.setenv("AGENTSUPPORT_CONTROL_PLANE_URL", "http://api:8000")
    monkeypatch.setenv("SESSION_RUNNER_TOKEN", "secret")
    monkeypatch.setenv("SESSION_RUNNER_ENDPOINT", "http://runner:8080")
    client = runner_registration_client_from_env(mode="deterministic")
    assert client is not None
    assert client.provider == "deterministic"
    assert client.endpoint == "http://runner:8080"
