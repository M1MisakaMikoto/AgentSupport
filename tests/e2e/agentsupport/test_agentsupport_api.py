import json

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.core_runtime import TraeCoreRunnerRuntime
from agentsupport.events import EventEnvelope
from agentsupport.runtime import DockerRuntimeDriver
from agentsupport.services import AgentSupportService
from session_runner.server import create_runner_app


@pytest.fixture
def service(tmp_path):
    return AgentSupportService(Settings(workspace_root=tmp_path))


@pytest.mark.asyncio
async def test_operational_endpoints_report_readiness_metrics_and_instance(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/live")
        ready = await client.get("/ready")
        metrics = await client.get("/metrics")

    assert live.json() == {"status": "ok"}
    assert ready.json()["status"] == "ready"
    assert ready.json()["instance_id"] == service.instance_id
    assert live.headers["X-AgentSupport-Instance"] == service.instance_id
    assert "agentsupport_active_runtimes 0\n" in metrics.text
    assert "agentsupport_queue_ready 0\n" in metrics.text
    assert "agentsupport_claims_expired 0\n" in metrics.text
    assert "agentsupport_outbox_publication_lag_seconds 0\n" in metrics.text
    assert "agentsupport_runner_reconciliation_needed 0\n" in metrics.text
    assert "agentsupport_workspace_lease_contention 0\n" in metrics.text
    assert metrics.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_workspace_session_conversation_and_idempotency(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        workspace = await client.post(
            "/workspaces", json={"name": "demo"}, headers={"Idempotency-Key": "w1"}
        )
        assert workspace.status_code == 201
        duplicate = await client.post(
            "/workspaces", json={"name": "demo"}, headers={"Idempotency-Key": "w1"}
        )
        assert duplicate.json()["id"] == workspace.json()["id"]
        session = await client.post("/sessions", json={"workspace_id": workspace.json()["id"]})
        assert session.status_code == 201
        conversation = await client.post(
            f"/sessions/{session.json()['id']}/conversations", json={"task": "write a file"}
        )
        assert conversation.status_code == 201
        assert conversation.json()["run"]["state"] == "RUNNING"
        events = await client.get(f"/conversations/{conversation.json()['id']}/events")
        assert [event["seq"] for event in events.json()] == [1, 2]
        conflict = await client.post(
            "/sessions", json={"workspace_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert conflict.status_code == 404
        assert conflict.json()["operation"] == "POST /sessions"
        assert conflict.json()["correlation_id"] == conflict.headers["X-Correlation-ID"]


@pytest.mark.asyncio
async def test_debug_acceptance_ui_is_served(service):
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/debug/")
        script = await client.get("/debug/app.js")

    assert response.status_code == 200
    assert "AgentSupport" in response.text
    assert 'aria-label="控制台导航"' in response.text
    assert 'id="control-console-link" class="nav-link"' in response.text
    assert 'class="nav-link active" href="./" aria-current="page"' in response.text
    assert 'id="continue-form"' in response.text
    assert 'id="continue-text"' in response.text
    assert 'id="parent-id"' in response.text
    assert script.status_code == 200
    assert "EventSource" in script.text
    assert "projectRunState" in script.text
    assert "continueRun" in script.text


@pytest.mark.asyncio
async def test_conversation_sse_replays_events_after_cursor(service):
    workspace = service.create_workspace("sse")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "run")
    app = create_app(service)

    async def finite_stream(conversation_id, after_seq=0):
        for event in service.events(conversation_id, after_seq):
            yield event

    service.events_store.stream = finite_stream
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/conversations/{conversation.id}/events/stream?after_seq=1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = response.text.splitlines()
    assert lines[0] == "id: 2"
    payload = json.loads(lines[1].removeprefix("data: "))
    assert payload["seq"] == 2


@pytest.mark.asyncio
async def test_approval_wait_pause_and_resume(service):
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "task")
    service.request_interaction(
        conversation.id, {"interaction_id": "approval-1", "kind": "approval", "tool": "file.write"}
    )
    assert conversation.run.state == "WAITING_INPUT"
    await service.pause(conversation.id)
    assert conversation.run.state == "PAUSED"
    await service.submit_approval(conversation.id, "approval-1", "APPROVE_ONCE")
    assert conversation.run.state == "RUNNING"
    assert conversation.run.checkpoint_id is not None


@pytest.mark.asyncio
async def test_agentsupport_checkpoint_preserves_pending_tool_batch(service):
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "tool task")
    service.request_interaction(
        conversation.id,
        {
            "interaction_id": "approval-1",
            "kind": "approval",
            "tool_batch": {
                "calls": [
                    {"call_id": "write-1", "name": "workspace.write", "arguments": {"value": "x"}}
                ]
            },
            "tool_batch_hash": "hash-from-core",
        },
    )
    checkpoint = service.create_checkpoint(conversation.id, "test")
    assert checkpoint.pending_tool_calls[0]["call_id"] == "write-1"
    assert checkpoint.tool_batch_hash


@pytest.mark.asyncio
async def test_stale_expected_seq_is_rejected(service):
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "task")
    service.request_interaction(conversation.id, {"interaction_id": "input-1", "kind": "question"})
    with pytest.raises(Exception) as error:
        await service.submit_input(conversation.id, "input-1", "answer", expected_seq=0)
    assert getattr(error.value, "code", None) == "CONFLICT"


@pytest.mark.asyncio
async def test_workspace_write_lease_serializes_sessions(tmp_path):
    service = AgentSupportService(Settings(workspace_root=tmp_path, max_active_sessions=2))
    workspace = service.create_workspace("shared")
    first = service.create_session(workspace.id)
    second = service.create_session(workspace.id)
    running = await service.create_conversation(first.id, "first")
    queued = await service.create_conversation(second.id, "second")
    assert running.run.state == "RUNNING"
    assert queued.run.state == "QUEUED"
    assert service.workspace_leases[workspace.id] == first.id
    await service.cancel(running.id)
    assert queued.run.state == "RUNNING"
    assert service.workspace_leases[workspace.id] == second.id


@pytest.mark.asyncio
async def test_conversation_queue_limit_returns_resource_exhausted(tmp_path):
    service = AgentSupportService(
        Settings(workspace_root=tmp_path, max_active_sessions=1, max_queued_conversations=1)
    )
    workspace = service.create_workspace("queue-limit")
    first = service.create_session(workspace.id)
    second = service.create_session(workspace.id)
    third = service.create_session(workspace.id)
    await service.create_conversation(first.id, "ask: active")
    queued = await service.create_conversation(second.id, "ask: queued")
    assert queued.run.state == "QUEUED"

    with pytest.raises(Exception) as error:
        await service.create_conversation(third.id, "ask: rejected")
    assert getattr(error.value, "code", None) == "RESOURCE_EXHAUSTED"
    assert getattr(error.value, "status_code", None) == 429
    assert not any(item.task == "ask: rejected" for item in service.conversations.values())


@pytest.mark.asyncio
async def test_container_start_failure_is_recorded_as_structured_run_failure(tmp_path):
    class FailedStartDriver(DockerRuntimeDriver):
        async def start(self, *args, **kwargs):
            raise TimeoutError("docker start timed out")

    service = AgentSupportService(Settings(workspace_root=tmp_path))
    service.runtime_driver = FailedStartDriver()
    workspace = service.create_workspace("start-failure")
    session = service.create_session(workspace.id)

    conversation = await service.create_conversation(session.id, "must fail")

    assert conversation.run.state.value == "FAILED"
    assert service.events(conversation.id)[-1].type == "run.failed"
    assert service.events(conversation.id)[-1].payload["code"] == "CONTAINER_START_TIMEOUT"


@pytest.mark.asyncio
async def test_agentsupport_dispatches_to_runner_and_releases_completed_session(service):
    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    workspace = service.create_workspace("core")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "complete normally")
    assert conversation.run.state == "COMPLETED"
    assert session.active_container_id is None
    assert service.events(conversation.id)[-1].type == "run.completed"


@pytest.mark.asyncio
async def test_agentsupport_maps_persisted_workspace_path_into_shared_runner(tmp_path):
    service = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "storage",
            core_runner_workspace_root="/workspace-data",
        )
    )
    workspace = service.create_workspace("mapped")
    physical = tmp_path / "storage" / "physical-folder"
    physical.mkdir()
    service.workspaces[workspace.id] = workspace.model_copy(update={"root_path": str(physical)})
    captured = {}

    class CapturingRuntime:
        async def run(self, request, sink):
            captured.update(request)
            await sink(
                EventEnvelope(
                    run_id=request["run_id"],
                    seq=1,
                    type="run.completed",
                    payload={"result": {"status": "completed"}},
                    source="test",
                )
            )

    service.core_runtime = CapturingRuntime()
    session = service.create_session(workspace.id)
    await service.create_conversation(session.id, "mapped path")
    assert captured["workspace_ref"] == "/workspace-data/physical-folder"


@pytest.mark.asyncio
async def test_agentsupport_keeps_session_while_runner_waits_for_input(service):
    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    workspace = service.create_workspace("waiting")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "ask: continue?")
    assert conversation.run.state == "WAITING_INPUT"
    assert conversation.run.pending_interaction is not None
    assert session.active_container_id is not None
    interaction_id = conversation.run.pending_interaction["interaction_id"]
    conversation = await service.submit_input(conversation.id, interaction_id, "continue")
    assert conversation.run.state == "COMPLETED"
    assert session.active_container_id is None
    assert [event.type for event in service.events(conversation.id)][-3:] == [
        "interaction.input",
        "message",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_agentsupport_forwards_approval_to_runner(service):
    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    workspace = service.create_workspace("approval")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "ask: approve tool?")
    approval_id = conversation.run.pending_interaction["interaction_id"]
    conversation = await service.submit_approval(conversation.id, approval_id, "APPROVE_ONCE")
    assert conversation.run.state == "COMPLETED"
    assert conversation.run.result_summary["approval"] == "APPROVE_ONCE"
    assert [event.type for event in service.events(conversation.id)][-2:] == [
        "approval.decided",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_input_idempotency_prevents_duplicate_events(service):
    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    workspace = service.create_workspace("input-idempotency")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "ask: answer")
    interaction_id = conversation.run.pending_interaction["interaction_id"]
    first = await service.submit_input(
        conversation.id, interaction_id, "yes", idempotency_key="input-1"
    )
    second = await service.submit_input(
        conversation.id, interaction_id, "yes", idempotency_key="input-1"
    )
    assert first.id == second.id
    assert [event.type for event in service.events(conversation.id)].count("interaction.input") == 1
    with pytest.raises(Exception) as error:
        await service.submit_input(
            conversation.id, interaction_id, "different", idempotency_key="input-1"
        )
    assert getattr(error.value, "code", None) == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_agentsupport_forwards_cancel_to_runner(service):
    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    workspace = service.create_workspace("cancel")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "ask: cancel me")
    conversation = await service.cancel(conversation.id, idempotency_key="cancel-1")
    duplicate = await service.cancel(conversation.id, idempotency_key="cancel-1")
    assert conversation.run.state == "CANCELLED"
    assert conversation.run.pending_interaction is None
    assert duplicate.id == conversation.id
    assert duplicate.run.pending_interaction is None
    assert service.events(conversation.id)[-1].type == "run.cancelled"
    assert service.events(conversation.id)[-1].source == "session_runner"
    assert [event.type for event in service.events(conversation.id)].count("run.cancelled") == 1


@pytest.mark.asyncio
async def test_local_cancel_clears_pending_interaction(service):
    workspace = service.create_workspace("local-cancel")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "wait locally")
    service.request_interaction(conversation.id, {"interaction_id": "local-input", "kind": "input"})

    cancelled = await service.cancel(conversation.id)

    assert cancelled.run.state == "CANCELLED"
    assert cancelled.run.pending_interaction is None
    assert service.events(conversation.id)[-1].type == "run.cancelled"


@pytest.mark.asyncio
async def test_paused_runner_resumes_from_agentsupport_checkpoint(service):
    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    workspace = service.create_workspace("resume")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "ask: resume me")
    interaction_id = conversation.run.pending_interaction["interaction_id"]
    await service.pause(conversation.id, reason="test_timeout")
    assert conversation.run.state == "PAUSED"
    assert conversation.run.checkpoint_id is not None
    resumed = await service.submit_input(conversation.id, interaction_id, "resumed")
    assert resumed.run.state == "COMPLETED"
    event_types = [event.type for event in service.events(conversation.id)]
    assert event_types[-4:] == [
        "interaction.input",
        "checkpoint.restored",
        "message",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_agentsupport_checkpoint_uses_container_workspace_reference(service):
    workspace = service.create_workspace("checkpoint-path")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "task")
    service.request_interaction(
        conversation.id, {"interaction_id": "approval-1", "kind": "approval"}
    )

    checkpoint = service.create_checkpoint(conversation.id, "test")

    assert checkpoint.workspace_ref == "/workspace"
    assert checkpoint.context_bundle.workspace_ref == "/workspace"


@pytest.mark.asyncio
async def test_unconfirmed_container_stop_keeps_workspace_lease(tmp_path):
    class UnconfirmedStopDriver(DockerRuntimeDriver):
        async def stop(self, container_id, *, force=False):
            return False

    service = AgentSupportService(Settings(workspace_root=tmp_path))
    service.runtime_driver = UnconfirmedStopDriver()
    workspace = service.create_workspace("unconfirmed-stop")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "run")
    container_id = session.active_container_id
    await service.cancel(conversation.id)
    assert session.active_container_id == container_id
    assert service.workspace_leases[workspace.id] == session.id
    assert service.events(conversation.id)[-1].type == "container_stop_unconfirmed"


@pytest.mark.asyncio
async def test_waiting_input_timeout_is_paused(service):
    service.config.waiting_input_timeout_seconds = 0
    workspace = service.create_workspace("timeout")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "task")
    service.request_interaction(
        conversation.id, {"interaction_id": "input-timeout", "kind": "question"}
    )
    assert await service.pause_expired_waiting() == 1
    assert conversation.run.state == "PAUSED"
    assert [event.type for event in service.events(conversation.id)][-2:] == [
        "checkpoint.created",
        "run.paused",
    ]


@pytest.mark.asyncio
async def test_health_supervisor_requires_repeated_failures(tmp_path):
    class MissingDriver(DockerRuntimeDriver):
        async def inspect(self, container_id):
            return {"status": "missing"}

    service = AgentSupportService(Settings(workspace_root=tmp_path, health_failure_threshold=2))
    service.runtime_driver = MissingDriver()
    workspace = service.create_workspace("health")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "run")
    assert await service.supervise_active_sessions() == 0
    assert conversation.run.state == "RUNNING"
    assert await service.supervise_active_sessions() == 1
    assert conversation.run.state == "LOST"


@pytest.mark.asyncio
async def test_health_supervisor_releases_orphan_container_lease(tmp_path):
    service = AgentSupportService(Settings(workspace_root=tmp_path, health_failure_threshold=1))
    workspace = service.create_workspace("orphan")
    session = service.create_session(workspace.id)
    await service.create_conversation(session.id, "run")
    session.active_run_id = None
    assert session.active_container_id is not None
    assert await service.supervise_active_sessions() == 1
    assert session.active_container_id is None
    assert workspace.id not in service.workspace_leases


@pytest.mark.asyncio
async def test_health_supervisor_checks_dynamic_core_endpoint(tmp_path):
    service = AgentSupportService(Settings(workspace_root=tmp_path, health_failure_threshold=1))
    workspace = service.create_workspace("core-health")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "run")

    class UnreadyCore:
        def __init__(self):
            self.run_ids = []

        async def health(self, run_id=None):
            self.run_ids.append(run_id)
            return {"live": {"status": "ok"}, "ready": {"status": "starting"}}

    core = UnreadyCore()
    service.core_runtime = core

    assert await service.supervise_active_sessions() == 1
    assert core.run_ids == [conversation.run.run_id]
    assert conversation.run.state == "LOST"
    assert session.active_container_id is None
