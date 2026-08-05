import pytest
from httpx import ASGITransport

from agent_runner_contracts.events import EventEnvelope
from agentsupport.config import Settings
from agentsupport.coordination import JobState
from agentsupport.core_runtime import TraeCoreRunnerRuntime
from agentsupport.domain import (
    PresetDefinition,
    PresetSkill,
    PresetToolPolicy,
)
from agentsupport.reconciler import DistributedReconciler
from agentsupport.runtime import DockerRuntimeDriver
from agentsupport.services import AgentSupportService
from agentsupport.worker import DistributedWorker
from session_runner.server import create_runner_app


def _settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'distributed.db'}",
        persistence_mode="postgres",
        execution_mode="distributed",
        workspace_root=tmp_path / "workspaces",
        core_runner_url="http://runner",
        max_active_sessions=2,
        job_lease_seconds=30,
        job_heartbeat_seconds=1,
    )


def _worker(config, service):
    core = TraeCoreRunnerRuntime(
        "http://runner",
        transport=ASGITransport(app=create_runner_app()),
    )
    return DistributedWorker(
        config,
        repository=service.repository,
        runtime_driver=DockerRuntimeDriver(),
        core_runtime=core,
    )


class RecordingSkillProvider:
    def __init__(self):
        self.skill_ids: list[list[str]] = []

    def manifest(self, skill_ids):
        self.skill_ids.append(list(skill_ids))
        return [
            {
                "skill_id": skill_id,
                "content_hash": "x" * 64,
                "mount_path": f"/opt/agent-skills/{skill_id}",
            }
            for skill_id in skill_ids
        ]

    def read_only_mounts(self, skill_ids):
        self.skill_ids.append(list(skill_ids))
        return [
            (f"/tmp/{skill_id}", f"/opt/agent-skills/{skill_id}")
            for skill_id in skill_ids
        ]


class RecordingCoreRuntime:
    def __init__(self):
        self.requests: list[dict] = []

    def register_run_endpoint(self, run_id, endpoint) -> None:
        return None

    async def run(self, request, sink):
        self.requests.append(request)
        await sink(
            EventEnvelope(
                run_id=request["run_id"],
                seq=1,
                type="run.completed",
                payload={"result": {"status": "completed"}},
            )
        )


@pytest.mark.asyncio
async def test_stateless_api_instances_share_queued_state(tmp_path):
    config = _settings(tmp_path)
    first = AgentSupportService(config)
    second = AgentSupportService(config)
    workspace = first.create_workspace("shared-api", idempotency_key="workspace")
    session = second.create_session(workspace.id, idempotency_key="session")

    conversation = await first.create_conversation(
        session.id, "complete on another worker", idempotency_key="conversation"
    )

    assert conversation.run.state == "QUEUED"
    assert second._conversation(conversation.id).run.state == "QUEUED"
    assert [event.type for event in second.events(conversation.id)] == [
        "conversation.queued"
    ]


@pytest.mark.asyncio
async def test_worker_executes_and_releases_distributed_job(tmp_path):
    config = _settings(tmp_path)
    api = AgentSupportService(config)
    workspace = api.create_workspace("worker-run")
    session = api.create_session(workspace.id)
    conversation = await api.create_conversation(session.id, "finish")
    worker = _worker(config, api)

    assert await worker.run_once() is True

    persisted = api.repository.get_conversation(conversation.id)
    job = api.repository.get_execution_job(conversation.run.run_id)
    assert persisted.run.state == "COMPLETED"
    assert job.state == JobState.COMPLETED
    assert worker.owned == {}
    assert [event.type for event in api.events(conversation.id)] == [
        "conversation.queued",
        "run.started",
        "run.running",
        "run.started",
        "message",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_distributed_worker_uses_project_config(tmp_path):
    config = _settings(tmp_path)
    api = AgentSupportService(config)
    user = api.create_user("alice")
    preset = api.create_preset(
        user.id,
        "review",
        definition=PresetDefinition(
            skills=[PresetSkill(skill_id="review", enabled=True)],
            tool_policy=PresetToolPolicy(
                allowed_tools=["bash", "task_done"],
                approval_required_tools=["bash"],
            ),
        ),
    )
    project = api.create_project("shop", user.id, preset_id=preset.id)
    session = api.create_project_session(project.id)
    conversation = await api.create_conversation(session.id, "finish")
    assert conversation.run.state == "QUEUED"

    skills = RecordingSkillProvider()
    core = RecordingCoreRuntime()
    worker = DistributedWorker(
        config,
        repository=api.repository,
        runtime_driver=DockerRuntimeDriver(),
        core_runtime=core,
        skill_provider=skills,
    )

    assert await worker.run_once() is True

    persisted = api.repository.get_conversation(conversation.id)
    assert persisted.run.state == "COMPLETED"
    assert any(ids == ["review"] for ids in skills.skill_ids)
    request = core.requests[0]
    assert request["context_bundle"]["skill_manifest"][0]["skill_id"] == "review"
    assert request["tool_policy"]["allowed_tools"] == ["bash", "task_done"]
    assert request["tool_policy"]["approval_required_tools"] == ["bash"]


@pytest.mark.asyncio
async def test_waiting_run_accepts_command_from_another_api_instance(tmp_path):
    config = _settings(tmp_path)
    first_api = AgentSupportService(config)
    second_api = AgentSupportService(config)
    workspace = first_api.create_workspace("command-run")
    session = first_api.create_session(workspace.id)
    conversation = await first_api.create_conversation(session.id, "ask:provide input")
    worker = _worker(config, first_api)

    assert await worker.run_once() is True
    waiting = second_api._conversation(conversation.id)
    assert waiting.run.state == "WAITING_INPUT"
    assert waiting.run.checkpoint_id is not None
    interaction_id = waiting.run.pending_interaction["interaction_id"]

    await second_api.submit_input(
        conversation.id,
        interaction_id,
        "continue",
        expected_seq=waiting.run.last_seq,
        idempotency_key="input-once",
    )
    assert await worker.maintain_owned_once() == 1

    completed = second_api._conversation(conversation.id)
    assert completed.run.state == "COMPLETED"
    assert completed.run.pending_interaction is None
    assert first_api.repository.get_execution_job(conversation.run.run_id).state == JobState.COMPLETED
    assert worker.owned == {}


@pytest.mark.asyncio
async def test_waiting_run_scales_down_and_resumes_in_replacement_runner(tmp_path):
    config = _settings(tmp_path)
    api = AgentSupportService(config)
    workspace = api.create_workspace("paused-run")
    session = api.create_session(workspace.id)
    conversation = await api.create_conversation(session.id, "ask:pause and replace")
    worker = _worker(config, api)
    await worker.run_once()
    waiting = api._conversation(conversation.id)
    assert waiting.run.state == "WAITING_INPUT"

    reconcile_config = config.model_copy(update={"waiting_input_timeout_seconds": 0})
    reconciler = DistributedReconciler(
        reconcile_config,
        repository=api.repository,
        runtime_driver=worker.runtime_driver,
    )
    result = await reconciler.run_once()
    paused = api._conversation(conversation.id)

    assert result["paused"] == 1
    assert paused.run.state == "PAUSED"
    assert worker.runtime_driver._containers[worker.owned[conversation.run.run_id].endpoint.runtime_id][
        "status"
    ] == "exited"

    interaction_id = paused.run.pending_interaction["interaction_id"]
    resuming = await api.submit_input(
        conversation.id,
        interaction_id,
        "resume on replacement",
        expected_seq=paused.run.last_seq,
        idempotency_key="resume-input",
    )
    assert resuming.run.state == "RESUMING"

    worker.core_runtime = TraeCoreRunnerRuntime(
        "http://runner",
        transport=ASGITransport(app=create_runner_app()),
    )
    assert await worker.run_once() is True

    completed = api._conversation(conversation.id)
    event_types = [event.type for event in api.events(conversation.id)]
    assert completed.run.state == "COMPLETED"
    assert "run.paused" in event_types
    assert "run.resuming" in event_types
    assert "checkpoint.restored" in event_types
