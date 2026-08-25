"""Experiment 10: unbounded request bodies bloat the database.

``ConversationCreate.task`` has no maximum length and ``SessionCreate.metadata``
is unbounded, so a caller can push multi-megabyte payloads that are stored
verbatim in PostgreSQL. This experiment posts a 5 MiB task and a 5 MiB metadata
dict through the public API and measures what is stored; after the fix both are
rejected with 422.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

import httpx
from common import EXPERIMENT_DATABASE_URL, record_evidence, table
from sqlalchemy import text

from agentsupport.adapters.notification import InMemoryEventNotifier, InMemoryEventStore
from agentsupport.adapters.persistence.sqlalchemy.models import Base
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.runtime import DockerRuntimeDriver
from agentsupport.adapters.skills import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.serving.http.app import create_app

PAYLOAD = "x" * (5 * 1024 * 1024)  # 5 MiB


def _build_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="inline",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp10-ws-") as tmp:
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
    workspace = repo.create_workspace("exp10-ws", "exp10-ws", "h1", None)
    session = repo.create_session(workspace, "h2", None)
    service = _build_service(repo)
    app = create_app(service=service)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        task_response = await client.post(
            f"/sessions/{session.id}/conversations",
            json={"task": PAYLOAD},
        )
        metadata_response = await client.post(
            "/sessions",
            json={
                "workspace_id": str(workspace.id),
                "metadata": {"blob": PAYLOAD},
            },
        )
    metadata_session_id = (
        metadata_response.json().get("id")
        if metadata_response.status_code == 201
        else None
    )

    with repo.engine.connect() as conn:
        if task_response.status_code == 201:
            task_rows = conn.execute(
                text("SELECT id FROM conversations WHERE session_id=:s"),
                {"s": str(session.id)},
            ).fetchall()
            stored_task_len = conn.execute(
                text("SELECT octet_length(task) FROM conversations WHERE id=:id"),
                {"id": task_rows[0][0]},
            ).scalar_one()
        else:
            stored_task_len = None
        metadata_row = None
        if metadata_session_id is not None:
            metadata_row = conn.execute(
                text("SELECT octet_length(metadata::text) FROM sessions WHERE id=:id"),
                {"id": metadata_session_id},
            ).scalar_one()

    rows = [
        ["POST /sessions/{id}/conversations with 5 MiB task", task_response.status_code],
        ["stored task size (octet_length)", stored_task_len or 0],
        ["POST /sessions with 5 MiB metadata", metadata_response.status_code],
        ["stored metadata size (octet_length)", metadata_row or 0],
    ]
    markdown = "\n".join(
        [
            "## Unbounded request bodies",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix a 5 MiB body is accepted and stored. After the fix",
            "the API rejects it with 422 and nothing is written.",
        ]
    )
    record_evidence(
        "exp10_request_size_limits",
        phase,
        markdown,
        command=f"python devtools/experiments/exp10_request_size_limits.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
