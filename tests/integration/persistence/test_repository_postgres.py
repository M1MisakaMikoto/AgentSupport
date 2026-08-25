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
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentsupport.adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
from agentsupport.adapters.persistence.sqlalchemy.models import EvalRunCaseRow
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
