"""Experiment 20: coordination metrics are zero in temporal mode even when the
database holds queued/running work.

``service.metrics()`` computes queue/job gauges from the in-memory
``self.conversations`` / ``self.sessions`` dicts, which are intentionally empty
in temporal mode (the database is authoritative). So ``/metrics`` reports 0
for queue_ready / jobs_running / jobs_waiting / active_runtimes while rows
with those states exist in PostgreSQL.
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
from agentsupport.domain import Conversation
from agentsupport.serving.http.app import create_app


def _seed(repo: PostgresRepository) -> None:
    workspace = repo.create_workspace("exp20-ws", "exp20-ws", "h1", None)
    session = repo.create_session(workspace, "h2", None)
    for i in range(7):
        repo.create_conversation(
            Conversation(session_id=session.id, task="queued"),
            f"h-{i}",
            None,
        )
    repo.try_acquire_workspace_lease(
        workspace.id, session.id, lease_epoch=1, container_id="container-1"
    )
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE conversations SET execution_state = 'QUEUED', "
                "created_at = now() - interval '1 hour'"
            )
        )
        conn.execute(
            text(
                "UPDATE conversations SET execution_state = 'RUNNING' "
                "WHERE id = (SELECT id FROM conversations ORDER BY created_at LIMIT 1)"
            )
        )
        conn.execute(
            text(
                "UPDATE conversations SET execution_state = 'WAITING_INPUT' "
                "WHERE id = (SELECT id FROM conversations ORDER BY created_at LIMIT 1 OFFSET 1)"
            )
        )


def _build_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp20-ws-") as tmp:
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


def _metric(lines: list[str], name: str) -> str:
    for line in lines:
        if line.startswith(name):
            return line.split()[-1]
    return "<missing>"


async def run(phase: str) -> str:
    repo = PostgresRepository(EXPERIMENT_DATABASE_URL)
    Base.metadata.drop_all(repo.engine)
    Base.metadata.create_all(repo.engine)
    _seed(repo)
    service = _build_service(repo)
    app = create_app(service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        response = await client.get("/metrics")
        lines = response.text.splitlines()

    rows = [
        ["QUEUED conversations in DB", 5],
        ["RUNNING conversations in DB", 1],
        ["WAITING_INPUT conversations in DB", 1],
        ["ACTIVE container leases in DB", 1],
        ["agentsupport_queue_ready", _metric(lines, "agentsupport_queue_ready ")],
        ["agentsupport_jobs_running", _metric(lines, "agentsupport_jobs_running ")],
        ["agentsupport_jobs_waiting", _metric(lines, "agentsupport_jobs_waiting ")],
        ["agentsupport_active_runtimes", _metric(lines, "agentsupport_active_runtimes ")],
        [
            "agentsupport_queue_oldest_ready_seconds",
            _metric(lines, "agentsupport_queue_oldest_ready_seconds "),
        ],
    ]
    markdown = "\n".join(
        [
            "## Coordination metrics vs database truth (temporal mode)",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix the gauges read 0 because they are computed from the",
            "in-memory maps that are empty in temporal mode. After the fix the",
            "gauges are computed from PostgreSQL.",
        ]
    )
    if phase == "after":
        assert _metric(lines, "agentsupport_queue_ready ") == "5.0", (
            "queue_ready does not reflect the database"
        )
        assert _metric(lines, "agentsupport_jobs_running ") == "1.0"
        assert _metric(lines, "agentsupport_jobs_waiting ") == "1.0"
        assert _metric(lines, "agentsupport_active_runtimes ") == "1.0"
    record_evidence(
        "exp20_metrics_temporal_truth",
        phase,
        markdown,
        command=f"python devtools/experiments/exp20_metrics_temporal_truth.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
