"""Regression: TraeCoreRunnerRuntime reuses one HTTP client across calls."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from agentsupport.adapters.runner.http import TraeCoreRunnerRuntime


class CountingClient(httpx.AsyncClient):
    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        super().__init__(*args, **kwargs)


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/live":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready"})
        if path == "/runs":
            return httpx.Response(200, json={"status": "COMPLETED", "events": []})
        if path.endswith(("/input", "/cancel")):
            return httpx.Response(200, json={"status": "CANCELLED", "events": []})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_runtime_reuses_single_http_client(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", CountingClient)
    runtime = TraeCoreRunnerRuntime("http://runner", transport=_transport())
    run_id = uuid4()
    await runtime.health()
    await runtime.health(run_id)
    await runtime.run(
        {
            "run_id": str(run_id),
            "conversation_id": str(uuid4()),
            "session_id": str(uuid4()),
            "container_id": "c",
            "lease_epoch": 1,
            "fence_epoch": 0,
            "correlation_id": "x",
            "context_bundle": {"task": "t"},
            "tool_policy": {},
        },
        lambda _event: None,
    )
    await runtime.accept_input(run_id, "interaction-1", "value")
    await runtime.cancel(run_id)
    assert CountingClient.instances == 1
    await runtime.aclose()
