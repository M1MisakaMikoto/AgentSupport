import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agentsupport.config import Settings
from agentsupport.coordination import JobState
from agentsupport.domain import Conversation
from agentsupport.event_notifier import InMemoryEventNotifier
from agentsupport.event_publisher import OutboxPublisher
from agentsupport.reconciler import DistributedReconciler
from agentsupport.repository import PostgresRepository, StaleClaim
from agentsupport.runtime import DockerRuntimeDriver
from agentsupport.services import AgentSupportService


def _repository(tmp_path):
    return PostgresRepository(f"sqlite:///{tmp_path / 'coordination.db'}", create_schema=True)


def _queued_conversation(repository: PostgresRepository, name: str = "distributed"):
    workspace = repository.create_workspace(name, str(name), name, None)
    session = repository.create_session(workspace, f"session-{name}", None)
    conversation = Conversation(session_id=session.id, task=name)
    persisted, created = repository.create_conversation_and_enqueue(
        conversation, workspace.id, f"conversation-{name}", None
    )
    assert created is True
    return workspace, session, persisted


def test_claim_capacity_renew_register_and_finish(tmp_path):
    repository = _repository(tmp_path)
    _, _, conversation = _queued_conversation(repository)

    claim = repository.claim_next_job("worker-a", capacity=1)

    assert claim is not None
    assert claim.job.run_id == conversation.run.run_id
    assert claim.job.state == JobState.CLAIMED
    assert repository.claim_next_job("worker-b", capacity=1) is None
    renewed_until = repository.renew_job_claim(
        claim.job.run_id, "worker-a", claim.claim_token
    )
    assert renewed_until > datetime.now(UTC)

    endpoint = repository.register_runner(
        claim, "worker-a", "runtime-1", "http://runtime-1:8080"
    )
    assert endpoint.lease_epoch == claim.fence_epoch
    assert repository.get_execution_job(claim.job.run_id).state == JobState.RUNNING
    assert repository.get_runner_endpoint(claim.job.run_id) == endpoint

    finished = repository.finish_execution_job(
        claim.job.run_id, "worker-a", claim.claim_token, JobState.COMPLETED
    )
    assert finished.state == JobState.COMPLETED
    assert repository.queue_depth() == 0


def test_expired_claim_is_reassigned_and_old_owner_is_fenced(tmp_path):
    repository = _repository(tmp_path)
    _, _, conversation = _queued_conversation(repository)
    started = datetime.now(UTC)
    first = repository.claim_next_job(
        "worker-a", capacity=1, lease_seconds=5, now=started
    )
    assert first is not None

    second = repository.claim_next_job(
        "worker-b", capacity=1, lease_seconds=30, now=started + timedelta(seconds=6)
    )

    assert second is not None
    assert second.job.run_id == conversation.run.run_id
    assert second.claim_token == first.claim_token + 1
    assert second.fence_epoch == first.fence_epoch + 1
    with pytest.raises(StaleClaim):
        repository.renew_job_claim(
            first.job.run_id, "worker-a", first.claim_token, now=started + timedelta(seconds=7)
        )


def test_retry_limit_transitions_job_to_failed(tmp_path):
    repository = _repository(tmp_path)
    _, _, conversation = _queued_conversation(repository, "retry-limit")

    for attempt in range(1, 4):
        claim = repository.claim_next_job(f"worker-{attempt}", capacity=1)
        assert claim is not None
        result = repository.finish_execution_job(
            claim.job.run_id,
            f"worker-{attempt}",
            claim.claim_token,
            JobState.RETRY,
            retry_delay_seconds=0,
        )

    assert result.state == JobState.FAILED
    assert result.attempts == 3
    assert repository.get_execution_job(conversation.run.run_id).state == JobState.FAILED
    assert repository.claim_next_job("worker-after-limit", capacity=1) is None


def test_workspace_single_writer_blocks_other_session(tmp_path):
    repository = _repository(tmp_path)
    workspace = repository.create_workspace("shared", "shared", "workspace", None)
    first_session = repository.create_session(workspace, "session-1", None)
    second_session = repository.create_session(workspace, "session-2", None)
    first_conversation = Conversation(session_id=first_session.id, task="first")
    second_conversation = Conversation(session_id=second_session.id, task="second")
    repository.create_conversation_and_enqueue(
        first_conversation, workspace.id, "conversation-1", None
    )
    repository.create_conversation_and_enqueue(
        second_conversation, workspace.id, "conversation-2", None
    )

    first = repository.claim_next_job("worker-a", capacity=2)
    assert first is not None
    assert repository.claim_next_job("worker-b", capacity=2) is None

    repository.finish_execution_job(
        first.job.run_id, "worker-a", first.claim_token, JobState.COMPLETED
    )
    second = repository.claim_next_job("worker-b", capacity=2)
    assert second is not None
    assert second.job.run_id != first.job.run_id


def test_durable_command_idempotency(tmp_path):
    repository = _repository(tmp_path)
    _, _, conversation = _queued_conversation(repository)

    first = repository.enqueue_command(
        conversation.run.run_id, "cancel", {}, "cancel-once"
    )
    duplicate = repository.enqueue_command(
        conversation.run.run_id, "cancel", {}, "cancel-once"
    )

    assert first == duplicate
    with pytest.raises(Exception, match="idempotency key"):
        repository.enqueue_command(
            conversation.run.run_id, "input", {"value": "different"}, "cancel-once"
        )


@pytest.mark.asyncio
async def test_outbox_publication_is_durable_and_idempotent(tmp_path):
    repository = _repository(tmp_path)
    _, _, conversation = _queued_conversation(repository)
    notifier = InMemoryEventNotifier()
    config = Settings(
        database_url=f"sqlite:///{tmp_path / 'coordination.db'}",
        persistence_mode="postgres",
        execution_mode="distributed",
    )
    publisher = OutboxPublisher(config, repository=repository, notifier=notifier)

    assert await publisher.run_once() == 1
    assert await publisher.run_once() == 0
    assert repository.claim_outbox("another-publisher") == []
    assert repository.list_events(conversation.id)[0].type == "conversation.queued"


@pytest.mark.asyncio
async def test_event_stream_polls_database_when_notifier_is_unavailable(tmp_path):
    config = Settings(
        database_url=f"sqlite:///{tmp_path / 'notifier-fallback.db'}",
        persistence_mode="postgres",
        execution_mode="distributed",
        workspace_root=tmp_path / "workspaces",
        event_poll_interval_seconds=0.01,
    )
    service = AgentSupportService(config)
    workspace = service.create_workspace("notifier-fallback")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "wait for database event")
    claim = service.repository.claim_next_job("worker-a", capacity=1)
    assert claim is not None

    class UnavailableNotifier:
        async def wait(self, *args, **kwargs):
            raise ConnectionError("notification transport unavailable")

    service.event_notifier = UnavailableNotifier()

    async def append_later():
        await asyncio.sleep(0.03)
        service.repository.append_claimed_event(
            claim.job.run_id,
            "worker-a",
            claim.claim_token,
            "worker.progress",
            {"status": "polled"},
        )

    append_task = asyncio.create_task(append_later())
    stream = service.stream_events(conversation.id, after_seq=1)
    event = await asyncio.wait_for(anext(stream), timeout=1)
    await append_task
    await stream.aclose()

    assert event.type == "worker.progress"
    assert event.payload == {"status": "polled"}


@pytest.mark.asyncio
async def test_reconciler_expires_missing_runtime_for_worker_recovery(tmp_path):
    repository = _repository(tmp_path)
    workspace, session, conversation = _queued_conversation(repository)
    claim = repository.claim_next_job("worker-a", capacity=1)
    runtime = DockerRuntimeDriver()
    runtime_id = await runtime.start(
        session.id, workspace.root_path, claim.fence_epoch, workspace.id
    )
    repository.register_runner(
        claim, "worker-a", runtime_id, "http://missing-runner:8080"
    )
    await runtime.stop(runtime_id)
    config = Settings(
        database_url=f"sqlite:///{tmp_path / 'coordination.db'}",
        persistence_mode="postgres",
        execution_mode="distributed",
    )
    reconciler = DistributedReconciler(
        config, repository=repository, runtime_driver=runtime
    )

    result = await reconciler.run_once()
    replacement = repository.claim_next_job("worker-b", capacity=1)

    assert result == {
        "expired_runners": 0,
        "paused": 0,
        "missing": 1,
        "removed_orphans": 0,
    }
    assert replacement is not None
    assert replacement.job.run_id == conversation.run.run_id
    assert replacement.previous_state == JobState.RUNNING
    assert replacement.fence_epoch == claim.fence_epoch + 1
