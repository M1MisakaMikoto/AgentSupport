"""Real-PostgreSQL integration tests, gated by RUN_POSTGRES_INTEGRATION_TESTS.

Run against the Compose PostgreSQL (default localhost:5432/agentsupport)::

    docker compose up -d postgres
    $env:RUN_POSTGRES_INTEGRATION_TESTS="1"
    .venv\\Scripts\\python.exe -m pytest tests\\integration\\persistence\test_repository_postgres.py -q

These tests are skipped unless the gate variable is set so the regular suite
stays green without a database server.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentsupport.adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
from agentsupport.adapters.persistence.sqlalchemy.models import (
    EvalRunCaseRow,
    OutboxEventRow,
)
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.domain import Conversation, ExecutionState
from agentsupport.evaluation.domain import (
    EvalCase,
    EvalCaseResult,
    EvalDataset,
    EvalRun,
    Verdict,
)
from agentsupport.events import EventEnvelope

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION_TESTS") != "1",
    reason="set RUN_POSTGRES_INTEGRATION_TESTS=1 to run against the Compose PostgreSQL",
)

DATABASE_URL = os.getenv(
    "AGENTSUPPORT_DATABASE_URL",
    "postgresql+psycopg://agent:agent@localhost:5432/agentsupport?connect_timeout=5",
)


@pytest.fixture
def repository() -> PostgresRepository:
    return PostgresRepository(DATABASE_URL, create_schema=True)


def _request_hash() -> str:
    return uuid4().hex * 2


def test_repository_round_trip(repository):
    workspace = repository.create_workspace(
        "postgres-demo", "/workspace/postgres-demo", _request_hash(), f"w-{uuid4().hex}"
    )
    session = repository.create_session(
        workspace, _request_hash(), f"s-{uuid4().hex}"
    )
    conversation = Conversation(session_id=session.id, task="task")
    repository.create_conversation(conversation, _request_hash(), f"c-{uuid4().hex}")
    conversation.run.state = ExecutionState.RUNNING
    event = EventEnvelope(
        run_id=conversation.run.run_id,
        seq=1,
        type="run.started",
        occurred_at=datetime.now(UTC),
    )
    repository.append_event(conversation, event)

    reloaded = PostgresRepository(DATABASE_URL).get_conversation(conversation.id)

    assert reloaded is not None
    assert reloaded.run.state == ExecutionState.RUNNING
    assert [item.type for item in PostgresRepository(DATABASE_URL).list_events(conversation.id)] == [
        "run.started"
    ]


def test_eval_store_round_trip_and_upsert():
    store = SqlAlchemyEvalStore(DATABASE_URL, create_schema=True)
    dataset = EvalDataset(
        name=f"pg-dataset-{uuid4().hex[:8]}",
        workspace_id=uuid4(),
        baseline_version="v1",
    )
    case = EvalCase(dataset_id=dataset.id, task="task")
    dataset.cases.append(case)
    store.create_dataset(dataset)
    run = EvalRun(
        dataset_id=dataset.id,
        results=[
            EvalCaseResult(
                case_id=case.id,
                task="task",
                verdict="PASS",
                score=1.0,
                verifier_results=[
                    Verdict(verifier_id="v", status="PASS", score=1.0, reason="r")
                ],
            )
        ],
    )
    store.save_run(run)
    with Session(store.engine) as db:
        row = db.execute(
            select(EvalRunCaseRow).where(EvalRunCaseRow.run_id == str(run.id))
        ).scalar_one()
        first_row_id = row.id

    store.save_run(run)  # upsert keeps the row identity
    with Session(store.engine) as db:
        rows = db.execute(
            select(EvalRunCaseRow).where(EvalRunCaseRow.run_id == str(run.id))
        ).scalars()
        rows = list(rows)
    assert len(rows) == 1
    assert rows[0].id == first_row_id

    fresh = SqlAlchemyEvalStore(DATABASE_URL)  # simulate a restart
    reloaded = fresh.get_run(run.id)
    assert reloaded is not None
    assert reloaded.results[0].verdict == "PASS"


def test_run_cases_index_and_check_constraints():
    store = SqlAlchemyEvalStore(DATABASE_URL, create_schema=True)
    indexes = {item["name"] for item in inspect(store.engine).get_indexes("eval_run_cases")}
    assert "ix_eval_run_cases_case_id" in indexes

    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eval_run_cases "
                "(id, run_id, case_id, task, verdict, score, verifier_results, created_at) "
                "VALUES (:id, :run_id, :case_id, 'task', 'BOGUS', 0.0, '[]', :created_at)"
            ),
            {
                "id": str(uuid4()),
                "run_id": str(uuid4()),
                "case_id": str(uuid4()),
                "created_at": datetime.now(UTC),
            },
        )


def test_list_sessions_uses_batched_lease_lookup(repository):
    """list_sessions must not issue one container-lease query per session."""

    workspace = repository.create_workspace(
        "batch-ws", "/workspace/batch-ws", _request_hash(), f"bw-{uuid4().hex}"
    )
    for _ in range(10):
        repository.create_session(workspace, _request_hash(), f"bs-{uuid4().hex}")

    counts = {"selects": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            counts["selects"] += 1

    sa_event.listen(repository.engine, "before_cursor_execute", _count)
    try:
        sessions = repository.list_sessions()
    finally:
        sa_event.remove(repository.engine, "before_cursor_execute", _count)
    assert len(sessions) >= 10
    assert counts["selects"] <= 2, f"expected <=2 SELECTs, got {counts['selects']}"


def test_list_session_events_is_single_query(repository):
    workspace = repository.create_workspace(
        "events-ws", "/workspace/events-ws", _request_hash(), f"ew-{uuid4().hex}"
    )
    session = repository.create_session(workspace, _request_hash(), f"es-{uuid4().hex}")
    conversation = Conversation(session_id=session.id, task="events")
    persisted = repository.create_conversation(
        conversation, _request_hash(), f"ec-{uuid4().hex}"
    )
    event = EventEnvelope(
        run_id=persisted.run.run_id,
        seq=persisted.run.last_seq + 1,
        type="message",
        payload={},
    )
    repository.append_event(persisted, event)

    counts = {"selects": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            counts["selects"] += 1

    sa_event.listen(repository.engine, "before_cursor_execute", _count)
    try:
        events = repository.list_session_events(session.id)
    finally:
        sa_event.remove(repository.engine, "before_cursor_execute", _count)
    assert [item.type for item in events] == ["message"]
    assert counts["selects"] == 1


def test_outbox_stats_reports_pending(repository):
    baseline = repository.outbox_stats()["pending"]
    now = datetime.now(UTC)
    with repository.engine.begin() as conn:
        conn.execute(
            insert(OutboxEventRow),
            [
                {
                    "id": str(uuid4()),
                    "topic": "conversation.events",
                    "aggregate_id": str(uuid4()),
                    "payload": {"pending": True},
                    "created_at": now,
                    "published_at": None,
                },
                {
                    "id": str(uuid4()),
                    "topic": "conversation.events",
                    "aggregate_id": str(uuid4()),
                    "payload": {"pending": True},
                    "created_at": now,
                    "published_at": None,
                },
                {
                    "id": str(uuid4()),
                    "topic": "conversation.events",
                    "aggregate_id": str(uuid4()),
                    "payload": {"published": True},
                    "created_at": now,
                    "published_at": now,
                },
            ],
        )
    stats = repository.outbox_stats()
    assert stats["pending"] == baseline + 2
    assert stats["lag_seconds"] >= 0


def test_retention_indexes_exist(repository):
    expected = {
        ("outbox_events", "ix_outbox_events_published_at"),
        ("idempotency_keys", "ix_idempotency_keys_created_at"),
        ("conversation_checkpoints", "ix_conversation_checkpoints_created_at"),
        ("runtime_operations", "ix_runtime_operations_created_at"),
        ("container_leases", "ix_container_leases_expires_at"),
        ("workspace_write_leases", "ix_workspace_write_leases_expires_at"),
    }
    found = set()
    for table_name in {name for name, _index in expected}:
        for index in inspect(repository.engine).get_indexes(table_name):
            found.add((table_name, index["name"]))
    assert expected <= found


def test_conversation_state_counts_and_oldest_queued(repository):
    workspace = repository.create_workspace(
        "states-ws", "/workspace/states-ws", _request_hash(), f"sw-{uuid4().hex}"
    )
    session = repository.create_session(workspace, _request_hash(), f"ss-{uuid4().hex}")
    for i in range(3):
        conversation = Conversation(session_id=session.id, task=f"state-{i}")
        repository.create_conversation(conversation, _request_hash(), f"sc-{i}-{uuid4().hex}")
    with repository.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE conversations SET execution_state = 'QUEUED', "
                "created_at = now() - interval '2 hours'"
            )
        )
    counts = repository.conversation_state_counts()
    assert counts.get("QUEUED", 0) >= 3
    oldest = repository.oldest_queued_created_at()
    assert oldest is not None
    assert (datetime.now(UTC) - oldest).total_seconds() > 3600


def _is_partitioned(engine) -> bool:
    with engine.connect() as conn:
        relkind = conn.execute(
            text(
                "SELECT relkind FROM pg_class "
                "WHERE relname = 'conversation_events'"
            )
        ).scalar_one_or_none()
    return relkind == "p"


def _partitions(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'conversation_events' AND c.relispartition"
            )
        ).fetchall()
    return {row[0] for row in rows}


def test_append_creates_month_partition_when_partitioned(repository):
    if not _is_partitioned(repository.engine):
        pytest.skip("conversation_events is not partitioned in this database")
    workspace = repository.create_workspace(
        "part-ws", "/workspace/part-ws", _request_hash(), f"pw-{uuid4().hex}"
    )
    session = repository.create_session(workspace, _request_hash(), f"ps-{uuid4().hex}")
    conversation = Conversation(session_id=session.id, task="partition")
    persisted = repository.create_conversation(
        conversation, _request_hash(), f"pc-{uuid4().hex}"
    )
    event = EventEnvelope(
        run_id=persisted.run.run_id,
        seq=persisted.run.last_seq + 1,
        type="message",
        payload={},
        occurred_at=datetime(2030, 5, 15, tzinfo=UTC),
    )
    repository.append_event(persisted, event)
    assert "conversation_events_203005" in _partitions(repository.engine)


def test_append_creates_no_partition_on_plain_table(repository):
    if _is_partitioned(repository.engine):
        pytest.skip("conversation_events is partitioned in this database")
    workspace = repository.create_workspace(
        "plain-ws", "/workspace/plain-ws", _request_hash(), f"nw-{uuid4().hex}"
    )
    session = repository.create_session(workspace, _request_hash(), f"ns-{uuid4().hex}")
    conversation = Conversation(session_id=session.id, task="plain")
    persisted = repository.create_conversation(
        conversation, _request_hash(), f"nc-{uuid4().hex}"
    )
    event = EventEnvelope(
        run_id=persisted.run.run_id,
        seq=persisted.run.last_seq + 1,
        type="message",
        payload={},
        occurred_at=datetime(2030, 6, 15, tzinfo=UTC),
    )
    repository.append_event(persisted, event)
    assert "conversation_events_203006" not in _partitions(repository.engine)


def test_drop_idle_event_partitions_keeps_active_months(repository):
    if not _is_partitioned(repository.engine):
        pytest.skip("conversation_events is not partitioned in this database")
    workspace = repository.create_workspace(
        "drop-ws", "/workspace/drop-ws", _request_hash(), f"dw-{uuid4().hex}"
    )
    session = repository.create_session(workspace, _request_hash(), f"ds-{uuid4().hex}")

    def conversation(task: str) -> Conversation:
        return repository.create_conversation(
            Conversation(session_id=session.id, task=task),
            _request_hash(),
            f"dc-{task}-{uuid4().hex}",
        )

    terminal = conversation("terminal")
    active = conversation("active")
    for conv, month, state in (
        (terminal, 1, "COMPLETED"),
        (active, 2, "RUNNING"),
    ):
        fresh = repository.get_conversation(conv.id)
        assert fresh is not None
        event = EventEnvelope(
            run_id=fresh.run.run_id,
            seq=fresh.run.last_seq + 1,
            type="message",
            payload={},
            occurred_at=datetime(2024, month, 15, tzinfo=UTC),
        )
        repository.append_event(fresh, event)
        with repository.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE conversations SET execution_state = :state WHERE id = :id"
                ),
                {"state": state, "id": str(fresh.id)},
            )

    dropped = repository.drop_idle_event_partitions_before(
        cutoff=datetime(2024, 6, 1, tzinfo=UTC)
    )
    partitions = _partitions(repository.engine)
    # 2024-01 holds only terminal events -> dropped; 2024-02 holds an active
    # conversation's event -> kept.
    assert "conversation_events_202401" not in partitions
    assert "conversation_events_202402" in partitions
    assert dropped >= 1
