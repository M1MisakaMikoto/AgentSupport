"""Experiment 13: SSE streams are silent while idle.

The control-plane event streams only emit events; when a conversation/session
is idle the stream sends nothing, so proxies with read timeouts (or clients
that monitor liveness) cannot tell the stream is alive. This experiment opens
``/conversations/{id}/events/stream`` against an idle conversation and measures
the time to the first byte with a short keepalive interval configured.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from uuid import UUID

import httpx
import uvicorn
from common import EXPERIMENT_DATABASE_URL, record_evidence, table

from agentsupport.adapters.notification import InMemoryEventNotifier, InMemoryEventStore
from agentsupport.adapters.persistence.sqlalchemy.models import Base
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.runtime import DockerRuntimeDriver
from agentsupport.adapters.skills import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.domain import Conversation
from agentsupport.serving.http.app import create_app

KEEPALIVE_SECONDS = 1


def _seed(repo: PostgresRepository) -> UUID:
    workspace = repo.create_workspace("exp13-ws", "exp13-ws", "h1", None)
    session = repo.create_session(workspace, "h2", None)
    conversation = repo.create_conversation(
        Conversation(session_id=session.id, task="exp13"),
        "hash-exp13",
        None,
    )
    return conversation.id


def _build_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp13-ws-") as tmp:
        workspace_root = Path(tmp)
    workspace_root.mkdir(parents=True, exist_ok=True)
    skills_root = workspace_root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    return AgentSupportService(
        config,
        events_store=InMemoryEventStore(),
        event_notifier=InMemoryEventNotifier(),
        workspace_provider=LocalWorkspaceStorageDriver(workspace_root),
        skill_provider=LocalSkillProvider(skills_root),
        runtime_driver=DockerRuntimeDriver(),
        core_runtime=None,
        repository=repo,
        runner_registry=InMemoryRunnerRegistry(),
    )


async def run(phase: str) -> str:
    os.environ["AGENTSUPPORT_SSE_KEEPALIVE_SECONDS"] = str(KEEPALIVE_SECONDS)
    repo = PostgresRepository(EXPERIMENT_DATABASE_URL)
    Base.metadata.drop_all(repo.engine)
    Base.metadata.create_all(repo.engine)
    conversation_id = _seed(repo)
    service = _build_service(repo)
    app = create_app(service=service)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8770, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("runner uvicorn did not start")

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8770") as client:
        started = asyncio.get_running_loop().time()

        async def first_chunk() -> bytes:
            async for chunk in response.aiter_bytes():
                return chunk
            return b""

        try:
            async with client.stream(
                "GET", f"/conversations/{conversation_id}/events/stream"
            ) as response:
                first_chunk = await asyncio.wait_for(first_chunk(), timeout=3.0)
        except TimeoutError:
            first_chunk = None
        time_to_first_byte = asyncio.get_running_loop().time() - started
    server.should_exit = True
    await server_task

    observed = first_chunk.decode(errors="replace").strip() if first_chunk else "(no data)"
    rows = [
        ["configured keepalive interval", f"{KEEPALIVE_SECONDS}s"],
        ["time to first byte", f"{time_to_first_byte * 1000:.0f} ms"],
        ["first chunk content", observed],
    ]
    markdown = "\n".join(
        [
            "## SSE idle keepalive",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix an idle stream sends nothing (first byte never",
            "arrives within a 3s probe window). After the fix a keepalive",
            "comment frame arrives at the configured interval.",
        ]
    )
    if phase == "after":
        assert first_chunk is not None, "no keepalive frame arrived"
        assert b": keepalive" in first_chunk, f"unexpected frame: {first_chunk!r}"
    record_evidence(
        "exp13_sse_keepalive",
        phase,
        markdown,
        command=f"python devtools/experiments/exp13_sse_keepalive.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
