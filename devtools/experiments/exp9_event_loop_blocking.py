"""Experiment 9: synchronous DB reads inside SSE streams stall the event loop.

``stream_events`` / ``stream_session_events`` (temporal mode) call the sync
``PostgresRepository`` directly inside the async generator. A conversation
with a large event history makes every poll hold the whole event loop for the
query duration.

This experiment seeds one conversation with ~80k events and measures the
*event-loop stall* directly: a timer scheduled to fire after 100ms is delayed
by exactly the blocking query time when the loop is stalled, and fires on
schedule once the read moves to a worker thread.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

from common import EXPERIMENT_DATABASE_URL, record_evidence, table

from agentsupport.adapters.notification import InMemoryEventNotifier, InMemoryEventStore
from agentsupport.adapters.persistence.sqlalchemy.models import (
    Base,
    ConversationEventRow,
    ConversationRow,
)
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.runtime import DockerRuntimeDriver
from agentsupport.adapters.skills import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.domain import Conversation

EVENT_COUNT = 80_000


def _seed(repo: PostgresRepository) -> UUID:
    workspace = repo.create_workspace("exp9-ws", "exp9-ws", "h1", None)
    session = repo.create_session(workspace, "h2", None)
    conversation = repo.create_conversation(
        Conversation(session_id=session.id, task="exp9"),
        "hash-exp9",
        None,
    )
    run_id = str(conversation.run.run_id)
    cid = str(conversation.id)
    with repo.engine.begin() as conn:
        conn.execute(
            __import__("sqlalchemy").insert(ConversationEventRow),
            [
                {
                    "id": str(uuid4()),
                    "conversation_id": cid,
                    "run_id": run_id,
                    "seq": i,
                    "type": "message",
                    "payload": {"i": i},
                    "source": "experiment",
                    "occurred_at": conversation.created_at,
                }
                for i in range(1, EVENT_COUNT + 1)
            ],
        )
        conn.execute(
            __import__("sqlalchemy").update(ConversationRow)
            .where(ConversationRow.id == cid)
            .values(last_seq=EVENT_COUNT, execution_state="RUNNING")
        )
    return conversation.id


def _build_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp9-ws-") as tmp:
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

    loop = asyncio.get_running_loop()
    canary_fired_at: list[float] = []

    def canary() -> None:
        canary_fired_at.append(time.perf_counter())

    async def consume_first_event() -> None:
        async for _event in service.stream_events(conversation_id, after_seq=0):
            return

    loop.call_later(0.1, canary)  # should fire ~100ms after scheduling
    started = time.perf_counter()
    await consume_first_event()
    query_elapsed = time.perf_counter() - started
    await asyncio.sleep(0.01)  # let the (due) canary callback run before reading
    canary_delay = None
    if canary_fired_at:
        canary_delay = (canary_fired_at[0] - started) - 0.1

    rows = [
        ["events in conversation", EVENT_COUNT],
        ["stream first-poll duration (sync query)", f"{query_elapsed * 1000:.0f} ms"],
        [
            "timer scheduled for +100ms actually fired at",
            (
                f"{(canary_fired_at[0] - started) * 1000:.0f} ms"
                if canary_fired_at
                else "never"
            ),
        ],
        [
            "event-loop stall (timer overrun)",
            f"{canary_delay * 1000:.0f} ms" if canary_delay is not None else "n/a",
        ],
    ]
    markdown = "\n".join(
        [
            "## Event-loop stall by sync DB reads in SSE streams",
            table(rows, ["observation", "value"]),
            "",
            "A timer scheduled for +100ms is the canary: if the sync query holds",
            "the loop, the canary fires only after the query returns (stall ≈ query",
            "duration). After the fix (reads via `asyncio.to_thread`) the canary",
            "fires on schedule while the query runs in a worker thread.",
        ]
    )
    record_evidence(
        "exp9_event_loop_blocking",
        phase,
        markdown,
        command=f"python devtools/experiments/exp9_event_loop_blocking.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
