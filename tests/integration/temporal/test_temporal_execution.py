"""Integration tests for the Temporal execution backend (phase 0).

These tests need a reachable Temporal dev server (default localhost:7233) and
are skipped otherwise so the regular suite stays green.  Start one with::

    temporal server start-dev --headless --port 7233

The control plane runs in ``execution_mode=temporal`` against a SQLite
repository; a fake CoreRuntime plays the role of the session runner so the
test covers the full service -> workflow -> activity -> repository chain
without containers or a real model.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_runner_contracts.events import EventEnvelope
from agentsupport.adapters.notification import InMemoryEventStore, create_event_notifier
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.skills.local import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceProvider
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.domain import Checkpoint, ContextBundle, ExecutionState
from agentsupport.execution.temporal.activities import (
    ExecutionContext,
    execute_run,
    fail_run,
    resume_run,
    set_execution_context,
    start_runner,
    stop_runner,
)
from agentsupport.execution.temporal.client import TemporalRunCoordinator
from agentsupport.execution.temporal.workflows import RunSessionWorkflow


def _ev(run_id: UUID, event_type: str, payload: dict) -> EventEnvelope:
    return EventEnvelope(
        run_id=run_id, seq=0, type=event_type, payload=payload, source="runner"
    )


class FakeCoreRuntime:
    """Simulates the session runner contract used by the activities."""

    def __init__(self, *, gate: bool = True, block_until_cancelled: bool = False) -> None:
        self.gate = gate
        self.block_until_cancelled = block_until_cancelled
        self.run_calls = 0
        self.resume_calls = 0

    def _interaction(self, run_id: UUID) -> dict:
        return {
            "interaction_id": "interaction-1",
            "kind": "approval",
            "tool_batch_hash": "batch-1",
            "tool_batch": {"calls": []},
            "pending_tool_calls": [],
            "tool_policy": {
                "allowed_tools": ["bash"],
                "approval_required_tools": ["bash"],
            },
        }

    async def health(self, run_id=None):
        return {"live": {"status": "ok"}}

    async def model_connectivity(self):
        return {}

    async def run(self, request: dict, event_sink) -> dict:
        self.run_calls += 1
        run_id = UUID(request["run_id"])
        await event_sink(_ev(run_id, "run.started", {"conversation_id": request["conversation_id"]}))
        await event_sink(_ev(run_id, "run.running", {}))
        if self.block_until_cancelled:
            await asyncio.Event().wait()
            return {"status": "completed"}
        if self.gate:
            await event_sink(_ev(run_id, "interaction.requested", self._interaction(run_id)))
            return {"status": "waiting"}
        await event_sink(_ev(run_id, "run.completed", {"result": {"status": "completed"}}))
        return {"status": "completed"}

    async def checkpoint(self, run_id, reason):
        if not isinstance(run_id, UUID):
            run_id = UUID(run_id)
        return Checkpoint(
            conversation_id=UUID("00000000-0000-0000-0000-000000000000"),
            run_id=run_id,
            last_event_seq=0,
            context_bundle=ContextBundle(
                task="write a test",
                conversation_id=UUID("00000000-0000-0000-0000-000000000000"),
                workspace_ref="/workspace",
                recent_events=[],
                tool_policy={"allowed_tools": ["bash"]},
            ),
            pending_interaction=self._interaction(run_id),
            tool_batch_hash="batch-1",
            context_bundle_hash="test",
            workspace_ref="/workspace",
            workspace_write_lease_epoch=0,
        )

    async def resume(
        self, checkpoint, value, event_sink, *, command_id=None, runtime_context=None
    ) -> dict:
        self.resume_calls += 1
        await event_sink(
            _ev(checkpoint.run_id, "run.completed", {"result": {"status": "completed"}})
        )
        return {"status": "completed"}

    async def accept_input(self, run_id, interaction_id, value, *, command_id=None):
        return {}

    async def accept_approval(self, run_id, approval_id, decision, *, command_id=None):
        return {}

    async def cancel(self, run_id, *, command_id=None):
        return {}


@pytest.fixture
async def env(tmp_path):
    from temporalio.client import Client
    from temporalio.worker import Worker

    try:
        client = await Client.connect("localhost:7233", namespace="default")
    except Exception as exc:  # noqa: BLE001 - environment gate
        pytest.skip(f"Temporal dev server not reachable: {exc}")

    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=f"sqlite:///{tmp_path / 'temporal.db'}",
        auto_create_schema=True,
        temporal_host="localhost:7233",
        temporal_namespace="default",
        temporal_task_queue="agentsupport-test",
        core_runner_url="http://runner",
        workspace_root=tmp_path / "workspace",
        skills_root=tmp_path / "skills",
    )
    repository = PostgresRepository(config.database_url, create_schema=True)
    fake = FakeCoreRuntime()
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    context = ExecutionContext(
        config=config,
        repository=repository,
        core_runtime=fake,
    )
    context.skill_provider = LocalSkillProvider(skills_root)
    set_execution_context(context)

    service = AgentSupportService(
        config,
        events_store=InMemoryEventStore(),
        event_notifier=create_event_notifier(None),
        workspace_provider=LocalWorkspaceProvider(tmp_path / "workspace"),
        skill_provider=LocalSkillProvider(skills_root),
        runtime_driver=context.runtime_driver,
        core_runtime=fake,
        repository=repository,
        temporal=TemporalRunCoordinator(
            host=config.temporal_host,
            namespace=config.temporal_namespace,
            task_queue=config.temporal_task_queue,
            workflow_timeout_seconds=config.temporal_workflow_timeout_seconds,
        ),
    )
    worker = Worker(
        client,
        task_queue=config.temporal_task_queue,
        workflows=[RunSessionWorkflow],
        activities=[start_runner, execute_run, resume_run, stop_runner, fail_run],
    )
    worker_task = asyncio.create_task(worker.run())
    try:
        yield SimpleNamespace(
            config=config, service=service, repository=repository, fake=fake
        )
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await service.temporal.close()


async def _wait_state(env, conversation_id, state, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        conversation = env.repository.get_conversation(conversation_id)
        if conversation is not None and conversation.run.state == state:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"conversation never reached {state}")


async def _new_conversation(env, task: str):
    workspace = env.service.create_workspace(f"ws-{task}")
    session = env.service.create_session(workspace.id)
    return await env.service.create_conversation(session.id, task)


async def test_pause_resume_with_idempotent_approval(env):
    conversation = await _new_conversation(env, "write a test")
    run_id = conversation.run.run_id

    # 1. the run reaches the human gate; the workflow holds the pause
    await _wait_state(env, conversation.id, ExecutionState.WAITING_INPUT)
    # 1b. the workflow exposes its durable pause via a query
    deadline = asyncio.get_running_loop().time() + 15
    status = {}
    while asyncio.get_running_loop().time() < deadline:
        status = await env.service.temporal.get_status(str(run_id))
        if status.get("status") == "waiting":
            break
        await asyncio.sleep(0.1)
    assert status.get("status") == "waiting"

    # 2. deliver the approval twice with the same idempotency key
    await env.service.submit_approval(
        conversation.id, "interaction-1", "APPROVE_ONCE", idempotency_key="key-1"
    )
    await env.service.temporal.submit_approval(
        str(run_id), "interaction-1", "REJECT", "key-1"
    )

    await _wait_state(env, conversation.id, ExecutionState.COMPLETED)
    assert env.fake.resume_calls == 1, "duplicate signal must not resume twice"

    events = env.repository.list_events(conversation.id)
    types = [event.type for event in events]
    assert types == [
        "run.started",
        "run.running",
        "interaction.requested",
        "approval.decided",
        "run.completed",
    ]


async def test_completion_without_gate(env):
    env.fake.gate = False
    conversation = await _new_conversation(env, "no gate")
    await _wait_state(env, conversation.id, ExecutionState.COMPLETED)
    events = env.repository.list_events(conversation.id)
    assert [event.type for event in events] == [
        "run.started",
        "run.running",
        "run.completed",
    ]


async def test_cancel_at_gate(env):
    conversation = await _new_conversation(env, "cancel at gate")
    await _wait_state(env, conversation.id, ExecutionState.WAITING_INPUT)
    await env.service.cancel(conversation.id)
    await _wait_state(env, conversation.id, ExecutionState.CANCELLED)
    events = env.repository.list_events(conversation.id)
    assert "run.cancelled" in [event.type for event in events]


async def test_cancel_mid_run(env, tmp_path):
    fake = FakeCoreRuntime(block_until_cancelled=True)
    config = env.config
    context = ExecutionContext(
        config=config,
        repository=env.repository,
        core_runtime=fake,
    )
    context.skill_provider = env.service.skill_provider
    set_execution_context(context)
    env.fake = fake

    conversation = await _new_conversation(env, "cancel mid run")
    await _wait_state(env, conversation.id, ExecutionState.RUNNING)
    await env.service.cancel(conversation.id)
    await _wait_state(env, conversation.id, ExecutionState.CANCELLED)
    events = env.repository.list_events(conversation.id)
    assert "run.cancelled" in [event.type for event in events]


async def test_activity_crash_retries_without_rerunning_completed_work(env, tmp_path):
    """An in-flight activity failure is retried; the workflow continues."""

    class CrashOnceRuntime:
        """Standalone fake: first run attempt crashes, retry completes."""

        def __init__(self):
            self.run_calls = 0

        async def run(self, request, event_sink):
            self.run_calls += 1
            if self.run_calls == 1:
                raise RuntimeError("simulated runner crash")
            run_id = UUID(request["run_id"])
            await event_sink(_ev(run_id, "run.started", {}))
            await event_sink(_ev(run_id, "run.running", {}))
            await event_sink(
                _ev(run_id, "run.completed", {"result": {"status": "completed"}})
            )
            return {"status": "completed"}

        async def checkpoint(self, run_id, reason):
            raise AssertionError("checkpoint must not be called for a completed run")

        async def resume(self, *args, **kwargs):
            raise AssertionError("resume must not be called for a completed run")

        async def health(self, run_id=None):
            return {"live": {"status": "ok"}}

        async def model_connectivity(self):
            return {}

        async def accept_input(self, *args, **kwargs):
            return {}

        async def accept_approval(self, *args, **kwargs):
            return {}

        async def cancel(self, *args, **kwargs):
            return {}

    fake = CrashOnceRuntime()
    context = ExecutionContext(
        config=env.config,
        repository=env.repository,
        core_runtime=fake,
    )
    context.skill_provider = env.service.skill_provider
    set_execution_context(context)
    env.fake = fake

    conversation = await _new_conversation(env, "crash once")
    await _wait_state(env, conversation.id, ExecutionState.COMPLETED)
    assert env.fake.run_calls == 2, "failed activity must be retried exactly once"
    events = env.repository.list_events(conversation.id)
    assert [event.type for event in events][-1] == "run.completed"


async def test_retry_exhaustion_marks_run_failed(env, tmp_path):
    """A permanently failing segment ends as run.failed, never stuck RUNNING."""

    class AlwaysFailingRuntime:
        async def run(self, request, event_sink):
            raise RuntimeError("runner keeps crashing")

        async def checkpoint(self, run_id, reason):
            raise AssertionError("checkpoint must not be called on a failing run")

        async def resume(self, *args, **kwargs):
            raise AssertionError("resume must not be called on a failing run")

        async def health(self, run_id=None):
            return {"live": {"status": "ok"}}

        async def model_connectivity(self):
            return {}

        async def accept_input(self, *args, **kwargs):
            return {}

        async def accept_approval(self, *args, **kwargs):
            return {}

        async def cancel(self, *args, **kwargs):
            return {}

    fake = AlwaysFailingRuntime()
    context = ExecutionContext(
        config=env.config,
        repository=env.repository,
        core_runtime=fake,
    )
    context.skill_provider = env.service.skill_provider
    set_execution_context(context)
    env.fake = fake

    conversation = await _new_conversation(env, "always fail")
    await _wait_state(env, conversation.id, ExecutionState.FAILED)

    persisted = env.repository.get_conversation(conversation.id)
    assert persisted is not None
    assert persisted.run.error is not None
    events = env.repository.list_events(conversation.id)
    assert events[-1].type == "run.failed"
    assert events[-1].payload["code"] == "WORKFLOW_FAILED"

    status = await env.service.temporal.get_status(str(conversation.run.run_id))
    assert status.get("status") == "failed"


async def test_temporal_mode_http_api_contract(env):
    """The public HTTP contract works unchanged in temporal mode."""

    from httpx import ASGITransport, AsyncClient

    from agentsupport.api import create_app

    app = create_app(env.service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workspace = (await client.post("/workspaces", json={"name": "api"})).json()
        assert "id" in workspace
        session = (
            await client.post("/sessions", json={"workspace_id": workspace["id"]})
        ).json()
        conversation = (
            await client.post(
                f"/sessions/{session['id']}/conversations",
                json={"task": "ask: approve tool?"},
            )
        ).json()
        conversation_id = conversation["id"]
        assert "run" in conversation

        deadline = asyncio.get_running_loop().time() + 30
        approval_id = None
        while asyncio.get_running_loop().time() < deadline:
            data = (await client.get(f"/conversations/{conversation_id}")).json()
            pending = data.get("run", {}).get("pending_interaction")
            if pending:
                approval_id = pending["interaction_id"]
                break
            await asyncio.sleep(0.1)
        assert approval_id is not None, "run never reached the human gate"

        response = await client.post(
            f"/conversations/{conversation_id}/approval",
            json={"approval_id": approval_id, "decision": "APPROVE_ONCE"},
        )
        assert response.status_code == 200

        while asyncio.get_running_loop().time() < deadline:
            data = (await client.get(f"/conversations/{conversation_id}")).json()
            if data.get("run", {}).get("state") == "COMPLETED":
                break
            await asyncio.sleep(0.1)

        events = (
            await client.get(f"/conversations/{conversation_id}/events")
        ).json()
        types = [event["type"] for event in events]
        assert types == [
            "run.started",
            "run.running",
            "interaction.requested",
            "approval.decided",
            "run.completed",
        ]
