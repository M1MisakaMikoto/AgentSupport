"""Experiment 21: list endpoints have no default size cap.

``GET /sessions`` without ``limit`` returns every row. A caller that forgets
the pagination parameters (or a script that used the old full-list behavior)
gets the whole table back: tens of thousands of rows and megabytes of JSON.
This experiment seeds 40k sessions and measures the no-parameter response
before and after introducing a configurable default limit.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
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
    with tempfile.TemporaryDirectory(prefix="exp21-ws-") as tmp:
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
        no_params = await client.get("/sessions")
        explicit = await client.get("/sessions", params={"limit": 1000})

    rows = [
        ["sessions in database", SESSION_COUNT],
        ["GET /sessions (no params) -> items", len(no_params.json())],
        ["GET /sessions (no params) -> bytes", len(no_params.content)],
        ["GET /sessions?limit=1000 -> items", len(explicit.json())],
    ]
    markdown = "\n".join(
        [
            "## List endpoint default limit",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix a no-parameter request returns the full table.",
            "After the fix it returns at most the configured default limit",
            "(`AGENTSUPPORT_LIST_DEFAULT_LIMIT`, default 100) while an explicit",
            "`limit` still overrides.",
        ]
    )
    if phase == "after":
        assert len(no_params.json()) == 100, (
            f"default limit returned {len(no_params.json())} items"
        )
        assert len(explicit.json()) == 1000, "explicit limit override broken"
    record_evidence(
        "exp21_list_default_limit",
        phase,
        markdown,
        command=f"python devtools/experiments/exp21_list_default_limit.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
