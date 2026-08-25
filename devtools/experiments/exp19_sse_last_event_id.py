"""Experiment 19: SSE streams ignore the Last-Event-ID reconnection header.

The event frames carry ``id: <seq>``, so a spec-compliant client reconnects
with ``Last-Event-ID: <seq>`` to resume. The route only reads the ``after_seq``
query parameter and ignores the header, so a reconnecting client gets the whole
history replayed from seq 1 instead of continuing.

This experiment seeds events 1..3 and opens the stream with
``Last-Event-ID: 2``, then records the first delivered frame.
"""

from __future__ import annotations

import argparse
import asyncio
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


def _seed(repo: PostgresRepository) -> UUID:
    workspace = repo.create_workspace("exp19-ws", "exp19-ws", "h1", None)
    session = repo.create_session(workspace, "h2", None)
    conversation = repo.create_conversation(
        Conversation(session_id=session.id, task="exp19"),
        "hash-exp19",
        None,
    )
    for seq in (1, 2, 3):
        event = __import__(
            "agent_runner_contracts.events", fromlist=["EventEnvelope"]
        ).EventEnvelope(
            run_id=conversation.run.run_id,
            seq=conversation.run.last_seq + 1,
            type="message",
            payload={"seq": seq},
        )
        repo.append_event(conversation, event)
        conversation.run.last_seq = event.seq
    return conversation.id


def _build_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp19-ws-") as tmp:
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
    repo = PostgresRepository(EXPERIMENT_DATABASE_URL)
    Base.metadata.drop_all(repo.engine)
    Base.metadata.create_all(repo.engine)
    conversation_id = _seed(repo)
    service = _build_service(repo)
    app = create_app(service=service)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8773, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start")

    first_frames: list[str] = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8773") as client, client.stream(
        "GET",
        f"/conversations/{conversation_id}/events/stream",
        headers={"Last-Event-ID": "2"},
    ) as response:
        iterator = response.aiter_bytes()
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=5.0)
            first_frames.append(chunk.decode(errors="replace"))
        except TimeoutError:
            pass
    server.should_exit = True
    try:
        await asyncio.wait_for(server_task, timeout=5)
    except (TimeoutError, asyncio.CancelledError):
        server_task.cancel()

    ids = []
    for frame in first_frames:
        for line in frame.splitlines():
            if line.startswith("id:"):
                ids.append(line)
    rows = [
        ["events in conversation", 3],
        ["request header Last-Event-ID", "2"],
        ["first delivered frame id(s)", ", ".join(ids) or "(none)"],
    ]
    markdown = "\n".join(
        [
            "## SSE Last-Event-ID resume",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix the header is ignored and the stream replays from",
            "seq 1 (first frame id: 1). After the fix the stream resumes after",
            "seq 2 (first frame id: 3).",
        ]
    )
    record_evidence(
        "exp19_sse_last_event_id",
        phase,
        markdown,
        command=f"python devtools/experiments/exp19_sse_last_event_id.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
