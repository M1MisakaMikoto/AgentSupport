"""Experiment 2: cost of warming the in-process event store for a conversation.

``AgentSupportService._conversation`` fills ``InMemoryEventStore`` by checking
``events_store.list(conversation.id, event.seq - 1)`` for every event, which is
O(events^2). This experiment times the fill for 500 / 1000 / 2000 events and
compares the scaling before and after the fix.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

from common import EXPERIMENT_DATABASE_URL, record_evidence, table
from sqlalchemy import insert, update

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


def _seed(repo: PostgresRepository, event_count: int) -> UUID:
    workspace = repo.create_workspace("exp2-ws", "exp2-ws", "h1", None)
    session = repo.create_session(workspace, "h2", None)
    conversation = repo.create_conversation(
        Conversation(session_id=session.id, task=f"exp2-{event_count}"),
        f"hash-{event_count}",
        None,
    )
    run_id = str(conversation.run.run_id)
    cid = str(conversation.id)
    with repo.engine.begin() as conn:
        conn.execute(
            insert(ConversationEventRow),
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
                for i in range(1, event_count + 1)
            ],
        )
        conn.execute(
            update(ConversationRow)
            .where(ConversationRow.id == cid)
            .values(last_seq=event_count, execution_state="RUNNING")
        )
    return conversation.id


def _fresh_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="inline",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp2-ws-") as tmp:
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
    # The constructor pre-loads everything; clear the caches so we measure the
    # per-conversation warm-up path in isolation.
    service.conversations.clear()
    service.events_store._events.clear()
    return service


def _measure(repo: PostgresRepository, conversation_id: UUID, event_count: int) -> tuple[float, int]:
    service = _fresh_service(repo)
    best = float("inf")
    for _ in range(3):
        service.events_store._events.clear()
        service.conversations.clear()
        started = time.perf_counter()
        service._conversation(conversation_id)
        best = min(best, time.perf_counter() - started)
    loaded = len(service.events_store.list(conversation_id))
    return best, loaded


def main(phase: str) -> None:
    repo = PostgresRepository(EXPERIMENT_DATABASE_URL)
    Base.metadata.drop_all(repo.engine)
    Base.metadata.create_all(repo.engine)

    rows: list[list[object]] = []
    ratios: list[str] = []
    for event_count in (500, 1000, 2000, 4000):
        conversation_id = _seed(repo, event_count)
        elapsed, loaded = _measure(repo, conversation_id, event_count)
        rows.append([event_count, f"{elapsed * 1000:.2f} ms", loaded])
        ratios.append(f"E={event_count}: t/E = {elapsed / event_count * 1e6:.2f} µs/event")

    markdown = [
        "## Warm-up cost vs event count (min of 3 runs)",
        table(rows, ["events (E)", "warm-up time", "events loaded"]),
        "",
        "Scaling diagnosis (`t/E` should stay flat for O(E), grow for O(E²)):",
        "",
        "\n".join(f"- {line}" for line in ratios),
        "",
        "```text",
        "O(E^2) expected when the fill loop checks events_store.list() per event;",
        "O(E) expected when the fill loop appends missing events in one pass.",
        "```",
    ]
    record_evidence(
        "exp2_event_load",
        phase,
        "\n".join(markdown),
        command=f"python devtools/experiments/exp2_event_load.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)
