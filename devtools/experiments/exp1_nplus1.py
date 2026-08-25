"""Experiment 1: query counts for list/event read paths.

Reproduces the N+1 query pattern in ``PostgresRepository.list_sessions`` /
``list_conversations`` and the full-table-scan + per-conversation query storm
in ``session_events`` (temporal mode). Run with ``--phase before|after``.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path
from uuid import UUID

from common import EXPERIMENT_DATABASE_URL, record_evidence, table
from sqlalchemy import event as sa_event

from agent_runner_contracts.events import EventEnvelope
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


class StatementCounter:
    def __init__(self) -> None:
        self.count = 0
        self.statements: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        normalized = statement.lstrip().lower()
        if normalized.startswith(("select", "with")):
            self.count += 1
            self.statements.append(statement.strip().splitlines()[0][:120])


def _reset_schema() -> PostgresRepository:
    engine = PostgresRepository(EXPERIMENT_DATABASE_URL).engine
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return PostgresRepository(EXPERIMENT_DATABASE_URL)


def _seed(repo: PostgresRepository) -> tuple[UUID, UUID, list[UUID]]:
    workspace = repo.create_workspace("nplus1-ws", "nplus1-ws", "h1", None)
    session_a = repo.create_session(workspace, "h2", None, session_id=None)
    session_b = repo.create_session(workspace, "h3", None, session_id=None)
    for _ in range(38):
        repo.create_session(workspace, None, None)
    conversation_ids: list[UUID] = []
    for i in range(15):
        conv = Conversation(session_id=session_a.id, task=f"task a {i}")
        persisted = repo.create_conversation(conv, f"hash-a-{i}", None)
        conversation_ids.append(persisted.id)
        event = EventEnvelope(
            run_id=persisted.run.run_id,
            seq=persisted.run.last_seq + 1,
            type="message",
            payload={"i": i},
            source="experiment",
        )
        repo.append_event(persisted, event)
    for i in range(15):
        conv = Conversation(session_id=session_b.id, task=f"task b {i}")
        persisted = repo.create_conversation(conv, f"hash-b-{i}", None)
        conversation_ids.append(persisted.id)
    return session_a.id, session_b.id, conversation_ids


def _build_service(repo: PostgresRepository) -> AgentSupportService:
    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=EXPERIMENT_DATABASE_URL,
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp1-ws-") as tmp:
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


def main(phase: str) -> None:
    repo = _reset_schema()
    session_a, session_b, _conversation_ids = _seed(repo)

    rows: list[list[object]] = []
    counter = StatementCounter()
    sa_event.listen(repo.engine, "before_cursor_execute", counter)

    counter.count = 0
    counter.statements.clear()
    sessions = repo.list_sessions()
    rows.append(["repository.list_sessions()", len(sessions), counter.count])
    list_sessions_queries = counter.statements.copy()

    counter.count = 0
    counter.statements.clear()
    conversations = repo.list_conversations()
    rows.append(["repository.list_conversations()", len(conversations), counter.count])
    list_conversations_queries = counter.statements.copy()

    counter.count = 0
    counter.statements.clear()
    filtered = repo.list_conversations(session_id=session_a)
    rows.append(["repository.list_conversations(session_id=A)", len(filtered), counter.count])

    service = _build_service(repo)
    counter.count = 0
    counter.statements.clear()
    events = service.session_events(session_a, after_seq=0)
    rows.append(["service.session_events(A, after_seq=0)", len(events), counter.count])
    session_events_queries = counter.statements.copy()

    # One polling cycle of the SSE path (temporal mode): the generator queries,
    # then waits on the notifier (0.25s). We bound the window so an idle
    # conversation (no events) does not hang the measurement.
    counter.count = 0
    counter.statements.clear()

    async def one_poll_cycle() -> tuple[int, bool]:
        collected = 0
        try:
            async with asyncio.timeout(0.35):
                async for _event in service.stream_session_events(session_b, after_seq=0):
                    collected += 1
        except TimeoutError:
            pass
        return collected, True

    collected, completed = asyncio.run(one_poll_cycle())
    rows.append(
        [
            "service.stream_session_events(B) one poll cycle",
            f"{collected} event(s), cycle {'' if completed else 'not '}bounded",
            counter.count,
        ]
    )

    markdown = [
        "## Query counts (statement counter, SELECTs only)",
        table(rows, ["operation", "rows returned", "SQL statements"]),
        "",
        "### sample queries — list_sessions",
        "```",
        "\n".join(list_sessions_queries[:4]),
        "```",
        "",
        "### sample queries — list_conversations",
        "```",
        "\n".join(list_conversations_queries[:4]),
        "```",
        "",
        "### sample queries — session_events",
        "```",
        "\n".join(session_events_queries[:6]),
        "```",
    ]
    record_evidence(
        "exp1_nplus1",
        phase,
        "\n".join(markdown),
        command=f"python devtools/experiments/exp1_nplus1.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)
