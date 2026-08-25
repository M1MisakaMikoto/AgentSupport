"""Experiment 17: the core-runtime HTTP adapter opens a client per call.

``TraeCoreRunnerRuntime`` constructs a fresh ``httpx.AsyncClient`` (and with it
a fresh connection pool) for every ``health`` / ``run`` / ``input`` / ``cancel``
call. The health supervisor polls every few seconds per active session, so this
is steady connection churn. This experiment counts client instantiations over
five calls.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

import httpx
from common import record_evidence, table

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
        if path.endswith("/input"):
            return httpx.Response(200, json={"status": "COMPLETED", "events": []})
        if path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "CANCELLED", "events": []})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def run(phase: str) -> str:
    original_client = httpx.AsyncClient
    httpx.AsyncClient = CountingClient  # type: ignore[assignment]
    runtime = TraeCoreRunnerRuntime("http://runner", transport=_transport())
    try:
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
        instances = CountingClient.instances
    finally:
        httpx.AsyncClient = original_client  # type: ignore[assignment]

    rows = [
        ["HTTP calls made", 5],
        ["httpx.AsyncClient instances created", instances],
    ]
    markdown = "\n".join(
        [
            "## Core-runtime HTTP client reuse",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix every call constructs a new AsyncClient (new",
            "connection pool). After the fix one client is reused.",
        ]
    )
    record_evidence(
        "exp17_httpx_client_reuse",
        phase,
        markdown,
        command=f"python devtools/experiments/exp17_httpx_client_reuse.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
