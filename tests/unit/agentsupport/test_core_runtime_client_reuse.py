"""Regression: TraeCoreRunnerRuntime reuses one HTTP client across calls."""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_failed_post_does_not_leak_the_event_subscription():
    """POST /runs 失败（runner 重启/连接被断）时不能留下订阅协程。

    实测踩到（2026-09-18）：订阅协程收不到取消信号时会一直重试，
    在日志里刷成"404 空转"的后台任务，直到进程重启。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready"})
        if path == "/runs":
            return httpx.Response(500, json={"detail": "runner is down"})
        # 订阅端点一直 404：修复前这里会变成无限重试的后台任务
        return httpx.Response(404)

    runtime = TraeCoreRunnerRuntime("http://runner", transport=httpx.MockTransport(handler))
    before = asyncio.all_tasks()
    with pytest.raises(httpx.HTTPStatusError):
        await runtime.run(
            {
                "run_id": str(uuid4()),
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
    await asyncio.sleep(0.3)
    leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
    assert not leaked, f"留下了没被收掉的协程：{leaked}"
    await runtime.aclose()
