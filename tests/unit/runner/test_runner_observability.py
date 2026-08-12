"""Runner observability: metrics endpoint and counters."""

import pytest
from httpx import ASGITransport, AsyncClient

from session_runner.metrics import record_http, record_mcp_connection, render_metrics
from session_runner.server import create_runner_app


def test_runner_metrics_render():
    record_http("POST", "/runs", 201)
    record_mcp_connection("connected")
    record_mcp_connection("failed")
    text = render_metrics()[0].decode()
    assert 'session_runner_http_requests_total{method="POST",route="/runs",status="201"}' in text
    assert 'session_runner_mcp_connections_total{result="connected"}' in text
    assert 'session_runner_mcp_connections_total{result="failed"}' in text


@pytest.mark.asyncio
async def test_runner_metrics_endpoint():
    app = create_runner_app(runner_mode="deterministic")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        await client.get("/live")
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'session_runner_http_requests_total{method="GET",route="/live"' in response.text
