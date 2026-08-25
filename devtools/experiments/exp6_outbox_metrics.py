"""Experiment 6: outbox metrics are hard-coded to zero.

``GET /metrics`` publishes ``outbox_pending`` / ``outbox_publication_lag_seconds``
from ``service.metrics()``, which returns hard-coded 0 even when the outbox has
unpublished rows. This experiment seeds pending outbox rows in PostgreSQL and
reads the Prometheus scrape to prove the gap, then re-checks after the fix.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from common import EXPERIMENT_DATABASE_URL, record_evidence, table

from agentsupport.adapters.notification import InMemoryEventNotifier, InMemoryEventStore
from agentsupport.adapters.persistence.sqlalchemy.models import Base, OutboxEventRow
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.runtime import DockerRuntimeDriver
from agentsupport.adapters.skills import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.serving.http.app import create_app


def _seed_outbox(repo: PostgresRepository) -> None:
    now = datetime.now(UTC)
    with repo.engine.begin() as conn:
        for i in range(5):
            conn.execute(
                __import__("sqlalchemy").insert(OutboxEventRow),
                {
                    "id": str(uuid4()),
                    "topic": "conversation.events",
                    "aggregate_id": str(uuid4()),
                    "payload": {"i": i},
                    "created_at": now,
                },
            )
        conn.execute(
            __import__("sqlalchemy").insert(OutboxEventRow),
            {
                "id": str(uuid4()),
                "topic": "conversation.events",
                "aggregate_id": str(uuid4()),
                "payload": {"old": True},
                "created_at": now - timedelta(hours=2),
            },
        )
        for _ in range(2):
            conn.execute(
                __import__("sqlalchemy").insert(OutboxEventRow),
                {
                    "id": str(uuid4()),
                    "topic": "conversation.events",
                    "aggregate_id": str(uuid4()),
                    "payload": {"published": True},
                    "created_at": now - timedelta(hours=1),
                    "published_at": now - timedelta(minutes=30),
                },
            )


def _metric(lines: list[str], name: str) -> str:
    for line in lines:
        if line.startswith(name):
            return line.split()[-1]
    return "<missing>"


def main(phase: str) -> None:
    repo = PostgresRepository(EXPERIMENT_DATABASE_URL)
    Base.metadata.drop_all(repo.engine)
    Base.metadata.create_all(repo.engine)
    _seed_outbox(repo)

    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp6-ws-") as tmp:
        workspace_root = Path(tmp)
    workspace_root.mkdir(parents=True, exist_ok=True)
    skills_root = workspace_root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    service = AgentSupportService(
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
    app = create_app(service=service)

    async def scrape() -> list[str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            response = await client.get("/metrics")
            response.raise_for_status()
            return response.text.splitlines()

    lines = asyncio.run(scrape())
    pending = _metric(lines, "agentsupport_outbox_pending ")
    lag = _metric(lines, "agentsupport_outbox_publication_lag_seconds ")
    rows = [
        ["unpublished outbox rows seeded", 6],
        ["agentsupport_outbox_pending scrape value", pending],
        ["oldest pending row age", "2h"],
        ["agentsupport_outbox_publication_lag_seconds scrape value", lag],
    ]
    markdown = "\n".join(
        [
            "## Outbox metrics from `GET /metrics`",
            table(rows, ["observation", "value"]),
            "",
            "Expected before fix: scrape values are `0.0` despite 6 pending rows.",
            "Expected after fix: `agentsupport_outbox_pending = 6.0` and lag > 0.",
        ]
    )
    if phase == "after":
        assert pending == "6.0", f"outbox_pending scrape: {pending}"
        assert lag != "0.0", "outbox lag is still hard-coded to 0"
    record_evidence(
        "exp6_outbox_metrics",
        phase,
        markdown,
        command=f"python devtools/experiments/exp6_outbox_metrics.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)
