from datetime import UTC, datetime

from agent_platform.config import Settings
from agent_platform.domain import Conversation, ExecutionState
from agent_platform.events import EventEnvelope
from agent_platform.repository import PostgresRepository
from agent_platform.services import PlatformService


def test_repository_does_not_auto_create_schema_when_disabled(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    PostgresRepository(database_url, create_schema=True)

    def unexpected_create_all(*args, **kwargs):
        raise AssertionError("schema creation must be owned by Alembic")

    monkeypatch.setattr("agent_platform.repository.Base.metadata.create_all", unexpected_create_all)
    repository = PostgresRepository(database_url, create_schema=False)

    repository.health_check()


def test_sqlalchemy_repository_persists_resources_events_and_leases(tmp_path):
    repository = PostgresRepository(f"sqlite:///{tmp_path / 'platform.db'}", create_schema=True)
    request_hash = "a" * 64
    workspace = repository.create_workspace("demo", "/workspace/demo", request_hash, "w1")
    duplicate = repository.create_workspace("demo", "/workspace/unused", request_hash, "w1")
    assert duplicate.id == workspace.id

    session = repository.create_session(workspace, "b" * 64, "s1")
    conversation = Conversation(session_id=session.id, task="task")
    repository.create_conversation(conversation, "c" * 64, "c1")
    repository.remember_idempotent(
        "input", "i1", "d" * 64, conversation.id, {"id": str(conversation.id)}
    )
    assert repository.find_idempotent("input", "i1", "d" * 64) == conversation.id
    conversation.run.state = ExecutionState.RUNNING
    event = EventEnvelope(
        run_id=conversation.run.run_id,
        seq=1,
        type="run.started",
        occurred_at=datetime.now(UTC),
    )
    repository.append_event(conversation, event)
    loaded = repository.get_conversation(conversation.id)
    assert loaded is not None
    assert loaded.run.state == ExecutionState.RUNNING
    assert loaded.run.last_seq == 1
    assert repository.list_events(conversation.id)[0].type == "run.started"

    assert repository.try_acquire_workspace_lease(workspace.id, session.id, 1, "container-1")
    assert repository.active_container_count() == 1
    repository.release_session_leases(session)
    assert repository.active_container_count() == 0
    operation_id = repository.create_runtime_operation("start", str(session.id))
    repository.finish_runtime_operation(operation_id, "SUCCEEDED", {"container_id": "container-1"})
    assert repository.list_workspaces()[0].id == workspace.id
    assert repository.list_sessions()[0].id == session.id
    assert repository.list_conversations()[0].id == conversation.id


async def test_platform_service_hydrates_events_after_restart(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'restart.db'}"
    config = Settings(
        workspace_root=tmp_path / "workspaces",
        database_url=database_url,
        persistence_mode="postgres",
    )
    first = PlatformService(config)
    workspace = first.create_workspace("restart")
    session = first.create_session(workspace.id)
    conversation = await first.create_conversation(session.id, "task")
    first.request_interaction(
        conversation.id, {"interaction_id": "restart-input", "kind": "question"}
    )

    restarted = PlatformService(config)
    loaded = restarted._conversation(conversation.id)
    assert loaded.run.state == ExecutionState.WAITING_INPUT
    assert [event.seq for event in restarted.events(conversation.id)] == [1, 2, 3]
    await restarted.submit_input(conversation.id, "restart-input", "continue")
    assert [event.seq for event in restarted.events(conversation.id)] == [1, 2, 3, 4]


async def test_cancel_clears_pending_interaction_after_repository_reload(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'cancel-restart.db'}"
    config = Settings(
        workspace_root=tmp_path / "workspaces",
        database_url=database_url,
        persistence_mode="postgres",
    )
    first = PlatformService(config)
    workspace = first.create_workspace("cancel-restart")
    session = first.create_session(workspace.id)
    conversation = await first.create_conversation(session.id, "cancel and reload")
    first.request_interaction(
        conversation.id, {"interaction_id": "cancel-input", "kind": "question"}
    )

    await first.cancel(conversation.id)
    restarted = PlatformService(config)
    loaded = restarted._conversation(conversation.id)

    assert loaded.run.state == ExecutionState.CANCELLED
    assert loaded.run.pending_interaction is None
    assert restarted.events(conversation.id)[-1].type == "run.cancelled"
