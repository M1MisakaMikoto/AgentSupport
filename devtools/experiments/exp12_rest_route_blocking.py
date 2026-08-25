"""Experiment 12: sync REST handlers block the event loop.

Most control-plane route handlers are ``async def`` but call synchronous
service/repository methods. A heavy read (e.g. ``GET /sessions`` over tens of
thousands of rows) therefore stalls the whole event loop. FastAPI/Starlette
runs plain ``def`` handlers in a worker thread, so converting the non-awaiting
handlers is the fix.

This experiment seeds 40k sessions and measures the event-loop stall (a
canary timer scheduled for +100ms) while ``GET /sessions`` is in flight.
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
    with tempfile.TemporaryDirectory(prefix="exp12-ws-") as tmp:
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

    loop = asyncio.get_running_loop()
    canary_fired_at: list[float] = []

    def canary() -> None:
        canary_fired_at.append(time.perf_counter())

    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        loop.call_later(0.1, canary)
        started = time.perf_counter()
        response = await client.get("/sessions")
        request_elapsed = time.perf_counter() - started
        await asyncio.sleep(0.01)

    canary_delay = None
    if canary_fired_at:
        canary_delay = (canary_fired_at[0] - started) - 0.1
    rows = [
        ["sessions in database", SESSION_COUNT],
        ["GET /sessions status", response.status_code],
        ["GET /sessions duration", f"{request_elapsed * 1000:.0f} ms"],
        [
            "canary timer scheduled for +100ms fired at",
            f"{(canary_fired_at[0] - started) * 1000:.0f} ms"
            if canary_fired_at
            else "never",
        ],
        [
            "event-loop stall (canary overrun)",
            f"{canary_delay * 1000:.0f} ms" if canary_delay is not None else "n/a",
        ],
    ]
    markdown = "\n".join(
        [
            "## Event-loop stall by sync REST handlers",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix the async handler blocks the loop for the whole",
            "request. After converting non-awaiting handlers to plain `def`",
            "(FastAPI worker thread), the canary fires on schedule.",
        ]
    )
    if phase == "after":
        assert canary_delay is not None and canary_delay < 0.5, (
            f"event-loop stall {canary_delay} seconds"
        )
    record_evidence(
        "exp12_rest_route_blocking",
        phase,
        markdown,
        command=f"python devtools/experiments/exp12_rest_route_blocking.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
