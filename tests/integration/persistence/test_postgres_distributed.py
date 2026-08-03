import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from agentsupport.coordination import JobState
from agentsupport.domain import Conversation
from agentsupport.repository import PostgresRepository

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_DISTRIBUTED_TESTS") != "1",
    reason="set RUN_POSTGRES_DISTRIBUTED_TESTS=1 with Compose PostgreSQL",
)


@pytest.fixture
def postgres_database_url():
    base_url = make_url(
        os.getenv(
            "POSTGRES_DISTRIBUTED_TEST_URL",
            "postgresql+psycopg://agent:agent@localhost:5432/agentsupport",
        )
    )
    database_name = f"agentsupport_distributed_{uuid4().hex}"
    admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    test_url = base_url.set(database=database_name)
    try:
        yield test_url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin.dispose()


def _enqueue(repository: PostgresRepository, name: str, workspace=None):
    workspace = workspace or repository.create_workspace(name, name, name, None)
    session = repository.create_session(workspace, f"session-{name}", None)
    conversation = Conversation(session_id=session.id, task=name)
    repository.create_conversation_and_enqueue(
        conversation, workspace.id, f"conversation-{name}", None
    )
    return conversation


def test_postgres_skip_locked_claims_and_workspace_fencing(postgres_database_url):
    setup = PostgresRepository(postgres_database_url, create_schema=True)
    first_conversation = _enqueue(setup, "first")
    second_conversation = _enqueue(setup, "second")
    first_repository = PostgresRepository(postgres_database_url)
    second_repository = PostgresRepository(postgres_database_url)
    barrier = Barrier(2)

    def claim_job(repository, worker):
        barrier.wait()
        return repository.claim_next_job(worker, capacity=2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                    lambda args: claim_job(*args),
                [(first_repository, "worker-a"), (second_repository, "worker-b")],
            )
        )

    assert all(claim is not None for claim in claims)
    assert {claim.job.run_id for claim in claims} == {
        first_conversation.run.run_id,
        second_conversation.run.run_id,
    }
    for claimed in claims:
        setup.finish_execution_job(
            claimed.job.run_id,
            claimed.job.claimed_by,
            claimed.claim_token,
            JobState.COMPLETED,
        )

    workspace = setup.create_workspace("shared", "shared", "shared", None)
    _enqueue(setup, "shared-first", workspace)
    _enqueue(setup, "shared-second", workspace)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        shared_claims = list(
            executor.map(
                    lambda args: claim_job(*args),
                [(first_repository, "worker-a"), (second_repository, "worker-b")],
            )
        )

    assert sum(item is not None for item in shared_claims) == 1


def test_postgres_concurrent_idempotent_create_returns_one_resource(postgres_database_url):
    PostgresRepository(postgres_database_url, create_schema=True)
    first = PostgresRepository(postgres_database_url)
    second = PostgresRepository(postgres_database_url)
    barrier = Barrier(2)

    def create(repository):
        barrier.wait()
        return repository.create_workspace(
            "same", f"path-{uuid4().hex}", "same-request-hash", "same-key"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        workspaces = list(executor.map(create, [first, second]))

    assert workspaces[0].id == workspaces[1].id
    assert len(first.list_workspaces()) == 1


def test_postgres_concurrent_event_append_preserves_sequence(postgres_database_url):
    setup = PostgresRepository(postgres_database_url, create_schema=True)
    conversation = _enqueue(setup, "event-sequence")
    claim = setup.claim_next_job("worker-a", capacity=1)
    assert claim is not None
    first = PostgresRepository(postgres_database_url)
    second = PostgresRepository(postgres_database_url)
    barrier = Barrier(2)

    def append(repository, event_type):
        barrier.wait()
        return repository.append_claimed_event(
            claim.job.run_id,
            "worker-a",
            claim.claim_token,
            event_type,
            {"source": event_type},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        appended = list(
            executor.map(
                lambda args: append(*args),
                [(first, "worker.event-a"), (second, "worker.event-b")],
            )
        )

    events = setup.list_events(conversation.id)
    assert [event.seq for event in events] == [1, 2, 3]
    assert {event.seq for event in appended} == {2, 3}
