"""Experiment 14: list endpoints have no pagination.

``GET /sessions`` (and the other list endpoints) return every row. A client
that sends ``?limit=100`` gets the parameter silently ignored and receives the
full table — tens of thousands of rows, megabytes of JSON, seconds of latency.
This experiment seeds 40k sessions and compares the response size/time of
``GET /sessions`` with and without ``?limit=100``.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from common import EXPERIMENT_DATABASE_URL, record_evidence, table
from sqlalchemy import insert

from agentsupport.adapters.notification import InMemoryEventNotifier, InMemoryEventStore
from agentsupport.adapters.persistence.sqlalchemy.models import Base, SessionRow
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.runtime import DockerRuntimeDriver
from agentsupport.adapters.skills import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.serving.http.app import create_app

SESSION_COUNT = 40_000


def _seed(repo: PostgresRepository) -> None:
    workspace_id = str(uuid4())
    now = datetime.now(UTC)
    with repo.engine.begin() as conn:
        conn.execute(
            insert(SessionRow),
            [
                {
                    "id": str(uuid4()),
                    "workspace_id": workspace_id,
                    "metadata": {},
                    "config": None,
                    "lease_epoch": 0,
                    "active_run_id": None,
                    "created_at": now,
                }
                for _ in range(SESSION_COUNT)
            ],
        )


def _build_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp14-ws-") as tmp:
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
    _seed(repo)
    service = _build_service(repo)
    app = create_app(service=service)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        started = time.perf_counter()
        limited = await client.get("/sessions", params={"limit": 100})
        limited_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        full = await client.get("/sessions")
        full_elapsed = time.perf_counter() - started

    limited_items = len(limited.json())
    full_items = len(full.json())
    rows = [
        ["sessions in database", SESSION_COUNT],
        [
            "GET /sessions?limit=100 -> items returned",
            limited_items,
        ],
        [
            "GET /sessions?limit=100 -> duration",
            f"{limited_elapsed * 1000:.0f} ms",
        ],
        [
            "GET /sessions?limit=100 -> response bytes",
            len(limited.content),
        ],
        ["GET /sessions (no params) -> items returned", full_items],
        ["GET /sessions (no params) -> duration", f"{full_elapsed * 1000:.0f} ms"],
        ["GET /sessions (no params) -> response bytes", len(full.content)],
    ]
    markdown = "\n".join(
        [
            "## List endpoint pagination",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix the client's `limit=100` is silently ignored and the",
            "full table is returned. After the fix `limit`/`offset` are honored.",
        ]
    )
    if phase == "after":
        assert limited_items == 100, f"limit=100 returned {limited_items} items"
    record_evidence(
        "exp14_list_pagination",
        phase,
        markdown,
        command=f"python devtools/experiments/exp14_list_pagination.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
